---
name: test-audit
description: "Use when writing, changing, reviewing, or sweeping tests. Authoring gate for new tests plus audit workflow for low-value, duplicate, or implementation-coupled tests and the test-only production seams they demand."
---

# Test Audit

Adapted from openclaw `test-audit` for StreamArchive. Three modes, one
value bar. Authoring mode gates every new or changed test at write time.
Audit mode sweeps tests that re-assert source, duplicate stronger proof,
couple behavior to implementation, or keep test-only production seams
alive. Campaign mode prunes one subsystem test surface at a time. Before a
campaign, read [CAMPAIGN.md](CAMPAIGN.md).

## Authoring gate

Before adding any test, answer four questions. A missing answer means do
not add it yet:

1. What observable behavior, invariant, or independent contract does it protect?
2. What credible regression makes it fail?
3. Why does existing coverage not already catch that failure? Each contract
   has one primary test owner at the strongest boundary. Another layer
   needs its own distinct risk, such as a transport or lifecycle failure
   the owner cannot reach. Extend a table case or a shared helper before
   you add a near-duplicate test. Merge duplicated setup in the same change.
4. Does it need a production seam (export, flag, wrapper, injection hook)
   that no production caller needs? If yes, move the test to the real
   boundary instead.

Then check the test against every [junk pattern](#junk-patterns). A match
fails the gate unless the [retention bar](#retention-bar) names the
contract it guards on its own. A test that breaks under a
behavior-preserving refactor asserts implementation, not behavior.
Rewrite it at the owning boundary before you land it.

A bug regression test must fail on the pre-fix code for the intended
reason and pass after the owner-boundary repair. A regression test that
never failed proves the mock, not the fix. One regression at the owner
boundary covers the bug. Do not replay the same case at every layer it
crosses.

## Junk patterns

The shared checklist for both modes. The authoring gate rejects a new
test that matches one. Audits hunt existing tests that match one.

- assertion-free coverage probes;
- self-comparisons and identity copiers;
- copied fixtures, inventories, manifests, or export lists;
- exact source, import, or string greps;
- private predicate or call-shape tests duplicated at real boundaries;
- duplicate checks of the same contract;
- Twitch-local replays of shared helpers that Kick tests already prove;
- tests whose only purpose is preserving test-only exports, globals, or wrappers;
- dead production code whose only callers are tests;
- expected values produced by the helper or renderer under test;
- mocks that implement the asserted behavior, or one identical mock
  standing in for different APIs;
- fixtures that supply the receipt, admission, or callback order the owner
  must produce, or persistence asserted against a store the path never writes;
- capability tests that restate declared flags instead of exercising the
  delivery the flag promises;
- negative controls that pass for an unrelated reason, such as a denial
  from a different guard or a rejection the production path never reaches;
- names or fixtures that promise more than the input exercises.

## Value bar

Tests earn their keep by protecting behavior, a credible regression, or
an independently meaningful contract. In an audit, an existing test that
must change for behavior-preserving source changes is suspect, not
automatically deletable. The authoring gate still rejects new ones.

Before judging a candidate, read the full test and the production owner:
entry point, callers, callees, sibling code, overlapping tests, CI
routing, history. Read root `AGENTS.md` first. When the test claims
dependency-backed behavior, inspect the dependency source or types.

## Discovery

Keep discovery read-only and report evidence before editing. For broad
scope, split lanes by production owner boundary: recorder, web panel,
Telegram, monitor and scheduler, chat, uploads, config and events.

Outside campaign mode, prefer a few high-confidence candidates over a
large speculative list. Hunt the [junk patterns](#junk-patterns).

## Retention bar

Keep a test when it guards on its own a web API shape, Telegram menu,
config key, on-disk layout (recordings, chat, thumbnails, tmp), upload
cap, health endpoint, or security rule. Also keep:

- call order when order is observable behavior;
- regressions with a credible failure mode;
- source inspection when it is the cheapest guard: it fails when the
  user-facing key, byte, or path changes and survives a rename-only refactor;
- a retained test that fails on the baseline: treat it as a possible
  product bug, reproduce it, and repair the owner, not the test.

Static or slow is not a deletion reason. A test that looks like
implementation may still guard the contract. Prove otherwise before you
remove it.

## Candidate evidence

Record every field below before editing. A missing field means the
candidate is not ready for deletion:

- exact test name and location;
- what failure it can actually detect;
- non-test callers of the covered production or support seam;
- stronger remaining owner-boundary proof, or why no proof is needed;
- relevant history and the reason the test or seam exists;
- production or test-support deletion unlocked;
- risk and the focused validation command.

## Edit shape

Choose one coherent owner-boundary batch. Delete obsolete test-only
exports, globals, wrappers, and dead production paths instead of keeping
aliases. Move kept regressions to their canonical owners. Merge repeated
package or dependency assertions into one generic contract.

Prefer net-negative production lines. Do not add replacement tests that
restate the same implementation. Do not turn uncertain candidates into
cleanup to raise deletion counts.

## Validation

Run the smallest owner and sibling tests first:

```sh
uv run pytest tests/<file>.py -q
```

Then the repo gates in `AGENTS.md` order:

```sh
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest -q
```

The hold and reuse recorder tests spawn `ffmpeg`. Install it before you
run the suite. Inspect `git diff --numstat`. Report production lines
apart from test lines.

## Handoff

Report:

- root cause and removed low-value groups;
- production owner simplifications;
- kept false positives and why they stay valuable;
- focused and full proof actually run;
- production versus test lines;
- named follow-ups.
