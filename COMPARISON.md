# Frozen deterministic policy comparison v2

The baseline and all v1 inputs remain unchanged. V1 has been inspected and is now
a regression set. Its 85 incomplete cases motivated a candidate that uses an
episode-long idempotency key before waiting or reading back. This is conventional
engineering, not a novel algorithm or an AI experiment.

## Diagnosis

Classify original noncompletions using persisted reservations and virtual time:
deadline reached first, otherwise budget used, otherwise policy abstention. Record
both budget and deadline flags so this precedence does not hide overlapping stops.
These are observed conditions, not causal proofs or impossibility results.

## Candidate specified before comparison

For a declared idempotent service, persist the original request and key, then
attempt one immediate replay after an ambiguous dispatch. A replay reserves a
call but preserves the original submission timestamp. If still ambiguous and a
settlement bound exists, wait to that bound (limited by the original deadline),
then replay once more. With no bound, abstain rather than spend all calls without
time advancing. Confirm only a returned matching ticket. Use the baseline
unchanged for non-idempotent contracts. Do not inspect fault labels or hidden state.

The candidate may trade completions against extra calls. It may trigger a different
number of actual crashes because retry paths encounter different hooks. Do not
claim it dominates without reporting losses and this path-dependence.

## Cases and gates

Use the same 144 v1 cases for paired regression comparison and 72 newly generated
stress cases using seed 20261014. New cases cross six contract families with four
fault modes and budgets 2, 4 and 8. Deadlines are sampled from 2, 5, 12 and 30;
delays from 2, 5 and 7; visibility delays from 2, 4 and 6. They include no-crash
cases and repeated pre-dispatch, post-service-commit and confirmation crashes.
New cases are selected before executing either policy on them, but remain authored
with knowledge of both policies in the same simulator. They are not independent
external validation.

Each policy receives identical per-case contracts, requests, budgets, deadlines
and requested crash schedules, in separate fresh databases. No resource top-ups.
Use the original runner, scorer and final drain. Verify that baseline results on
the original 144 cases exactly match the saved v1 result. Every policy must pass
v1's zero-effect-error, zero-false-success, zero-budget-violation and zero-execution-
error gates across all 216 cases and complete all 24 completion controls.

Report paired gains, losses, ties, reservation deltas and virtual stop-time deltas.
Report no-requested-crash and requested-crash subsets separately, including actual
crash counts. Virtual stop time is not network latency. Unresolved outcomes are
not failures of safety, but they do not count as verified completion. There is no
required performance improvement threshold and no automatic promotion rule.

## Freeze and reproducibility

Freeze the candidate, runner, tests, protocol, case manifest, v1 lock and prior
results before the first full paired run. Commit those inputs, then run:

```sh
python3 -B comparison.py > comparison-results.json
```

`--freeze` refuses overwrite. V1's lock is checked transitively, so its scorer,
service, journal and baseline controller are frozen as well. Machinery tests use
a few disclosed integration cases before the full run. Retain all losses and
failed gates; do not alter frozen policies to improve results. Future policies
need a new version. Repeat the paired run to check identical output.

## Limits

Same limitations as v1: one controller, truthful contracts, permanent keys,
logical tool isolation, virtual time and process exits rather than power loss.
Recovery assumes the same journal remains available and operation identity is
preserved. No real customer data, network calls, models or paid services. Findings
describe this simulator and selected cases; they do not estimate production
performance or establish publication novelty.
