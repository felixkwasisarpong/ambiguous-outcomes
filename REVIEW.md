# Independent review packet

Status: prepared for review. No outside person has reviewed or independently
validated this work. A clean-source reproduction by the same author is a
portability/reproducibility check, not independent scientific validation.

## Review target

The settled-absence comparison is specified in [RECONCILIATION.md](RECONCILIATION.md).
Its policy, service dependencies, scorer, cases and tests were frozen in commit
`2b9d380` before the full comparison. The frozen source hashes are in
[reconciliation-v1.lock.json](reconciliation-v1.lock.json); previous versions are
verified transitively. The current README records outcomes and limitations.

The narrow claim to examine is whether the new policy can recover specified
synthetic cases using settled negative readback without introducing duplicate
effects, false completion or resource-limit violations under the declared
contract. No novelty, production, AI-model or universal completion claim is made.

## Reproduce from a fresh clone

Use Python 3.10 or newer and a fresh checkout of this public repository. No third
party packages, API credentials, real data or paid services are needed. Record the
repository revision, Python version and operating system with your findings.

```sh
git clone https://github.com/felixkwasisarpong/ambiguous-outcomes.git
cd ambiguous-outcomes
python3 -B -m unittest -q
python3 -B -c 'import reconciliation_eval; reconciliation_eval.verify()'
python3 -B reconciliation_eval.py --output reviewer-reconciliation.json
cmp reviewer-reconciliation.json reconciliation-v1-results.json
```

Do not overwrite an attempt. Preserve any timeout, exception, mismatching case or
nonzero exit before retrying. The process timeout is 15 seconds; do not increase
it to make a run pass. Timing artifacts naturally differ, but these behavioral
reports should match exactly. The original guard must reproduce all 232 earlier
rows; no historical outcome rows are reused in the new run.

## Check the safety argument independently

The implementation under review is `reconciled_policy.py`, with reservations in
`recovery_journal.py`. The same-author reference is `readback_policy.py`. Both use
the unchanged finite-retention service and independent ledger scorer.

1. Does every same-key replay reach acceptance strictly before the earliest
   guaranteed key expiration, allowing for maximum request transit?
2. Does a negative read begin only after the LATEST possible dispatch plus
   maximum transit plus the settlement-and-visibility bound before it permits
   a new epoch? Using the first dispatch alone is insufficient.
3. Are budget reservation and dispatch evidence atomic? Check termination inside
   the transaction, after reservation but before dispatch, after readback but
   before reservation, and during confirmation. Could a stale proof survive a
   crash and authorize two different attempts?
4. Does the absence argument depend on exact readback, truthful bounded settlement,
   one writer and no in-flight unjournaled operation? Try constructing a pending
   effect that arrives after a read incorrectly considered conclusive.
5. Are original identity, payload, key, deadline and budget preserved when a new
   epoch starts? Can restart, missing metadata or a strategy switch bypass limits?
6. Does the scorer detect a duplicate that commits AFTER the controller stops,
   and reject a success label that refers to only one of multiple effects?
7. Is the TTL-aware reference a meaningful comparison? Report advantages and
   disadvantages of its readback-first approach; do not assess only the legacy
   policy that intentionally violates its own permanent-key assumptions.

## Challenge assumptions and broaden evaluation

Build an independently authored service or model from the written contract before
consulting the existing service implementation. Keep its expected outcomes separate
from policy code. Generate new cases and freeze them before observing results.
Share authorship, generation rules and any case-selection decisions explicitly.

Useful challenges include multiple ambiguous attempts, tighter budgets and
boundaries, delayed acceptance near TTL expiry, termination at each journal
boundary, unknown settlement bounds, and non-tight latency bounds. Distinguish
failures inside the declared contract from failures after relaxing it.

A separate extension is needed for clock drift, concurrent writers, early key
loss, bounded versus unbounded read staleness, authentication failures, changing
contracts and real network timing. The current full service injects a fault only
on the first new create; focused fake-tool tests exercise additional ambiguity,
but do not replace a systematic repeated-fault service evaluation.

Fourteen previously missed dropped/no-lookup cases are intentionally not promised
recoverable. The test `test_indistinguishable_expired_histories_require_different_retry_outcomes`
constructs equal controller-visible histories for dropped and committed-but-reply-lost
outcomes. A forced expired-key retry duplicates only the committed world. This is
a narrow witness under this API, not a proof of global policy optimality.

## Return findings in a review artifact

Include the reviewer identity/date, inspected revision, exact commands, environment,
case IDs, unmodified failed outputs and a concise counterexample for each finding.
Separate reproduction, contract correctness, implementation safety, baseline
fairness, external validity and literature novelty. Cite relevant prior work
before judging originality. A passing test suite alone is not sufficient.

This repository contains synthetic public research only. Private documents,
customer data and personal evidence are not needed for review. No review request
has been sent to anyone as part of preparing this packet.
