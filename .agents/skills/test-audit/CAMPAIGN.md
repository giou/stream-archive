# Test-pruning campaign

Campaign mode prunes one subsystem test surface in one change: the
recorder, the web panel, Telegram, or another core area. The value bar,
retention bar, candidate evidence, and validation in [SKILL.md](SKILL.md)
apply to every lane. This file adds the order of work. Each step ends on
its completion rule. Do not start the next step early.

## 1. Baseline

Record the subsystem test and support line counts and every in-scope
test file pass state at the pinned commit. Keep baseline failures in
their own list. A baseline failure may be a real product bug, not a
stale test.

Done when every in-scope test file has a recorded baseline result.

## 2. Lanes and inventory

Split the surface into **lanes** along production owner boundaries, not
file prefixes. For StreamArchive the lanes are recorder, web panel,
Telegram, monitor and scheduler, chat, uploads (MTProto and YouTube),
and config and events. Include the subsystem cases at shared core
boundaries. One lane per agent for parallel discovery.

Done when every test file the subsystem owns belongs to exactly one lane.

## 3. Read-only ledger per lane

Each lane agent reads every assigned test in full, including parameter
tables. It also reads the production owners and their entry points,
callers, history, and CI routing. Each test declaration goes into a
written **ledger** with one mark. A parametrized case is one declaration
unless its rows need different marks. Then mark each row.

- `R`: retain, naming the contract and the bug it catches;
- `F`: retain the contract but repair the assertion, such as a negative
  that passes when only one of several items is missing;
- `C`: consolidate, naming the owner that absorbs the assertion first: a
  sibling table case, a stronger boundary suite, or the shared owner;
- `D`: delete, naming the proof that remains, or why no contract exists.

Judge a test by its assertions, not its name.

Done when every declaration in the lane has a mark and an evidence line.

## 4. Layer plan per lane

Treat the ledger as input, not as the edit list. A second read-only
pass, starting from the ledger, looks for the redundant **layer**. Name
the **keeper** suite for each contract. Prefer the real transport
boundary with a fake feed over a mocked collaborator. Correct ledger
errors this pass finds.

Done when each lane plan names its retired files, its keeper per
contract, the assertions to carry into keepers, and the test-only
production seams unlocked.

## 5. Cutover

Edit lane by lane. With each lane, remove the test-only production
seams it unlocks: injection parameters, getters, reset exports, and
indirection layers. Validate each lane with the [SKILL.md](SKILL.md)
commands before moving on.

Done when every lane plan is applied and each lane keeper passes.

## 6. Preservation review

Before claiming completion, compare deleted coverage against the
keepers. Look for contracts that lost their only proof. Look for new
assertions that cannot fail, such as a rejection row the production
code never reaches.

For each restored contract, make one deliberate **mutation** of the
production owner and confirm the keeper goes red. Then restore the
source byte for byte.

Done when every reported gap is restored or rejected with source
evidence, and every restored contract has a caught mutation.

## 7. Product defects

A baseline failure that survives into a keeper is a bug report. Fix it
at its owner as a separate change, and prove it through the real user
flow, with a **control** run that reverts the fix and shows the old
behavior. Record unrelated product defects you find as follow-ups
instead of fixing them in the campaign.

Done when each repaired defect has a failing control and a passing
candidate on the same harness.

## 8. Reconcile and hand off

Merge `main` rather than rebasing a long campaign. When `main` changed
a file the campaign deleted, keep the deletion. Port the new contract
into the keeper instead, and confirm every new regression `main` added
still has a home. Rerun the whole suite on the merged head.

Hand off with the [SKILL.md](SKILL.md) report, plus:

- baseline and final test and support line counts, with production counted apart;
- lanes, retired layers, and keepers;
- preservation gaps found and their mutations;
- product defects with control and candidate proof.
