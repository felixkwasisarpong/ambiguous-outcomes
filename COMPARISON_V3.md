# Frozen three-policy comparison v3

V1 and v2 remain unchanged. The hybrid candidate attempts to repair all five v2
regressions while retaining its useful safe key retries. No successful outcome or
promotion is assumed. This is simulator engineering, not an AI or novelty claim.

## Hybrid policy, specified before the full run

Reuse the original request, key, operation identity, budget, submission timestamp
and deadline. For idempotent services, try an immediate replay after an ambiguous
dispatch, as v2 does. If still ambiguous, wait to the original settlement bound
when one exists, then prefer exact readback over another write if lookup is
supported. Missing readback permits a further same-key retry. Without a bound,
continue readback and safe key retries within the original persistent call budget
instead of abstaining immediately. No new allowance is created by a restart.
Non-idempotent services still use the baseline unchanged.

The controller receives public tools and its journal only. It cannot inspect fault
labels, future commits, expected answers or other policies' outcomes. Unknown-bound
pending actions may consume the budget without completion because virtual time does
not advance on a create or lookup. That limitation must remain visible in results.

## Cases and provenance

Use all 216 v2 cases, now explicitly development/regression cases, plus 72 new cases
seeded by 20261015. New cases cross six contract families with four fault modes and
budgets 3, 5 and 9. Deadlines are sampled from 1, 4, 10 and 25; commit delays from
1, 3 and 6; visibility delays from 1, 5 and 8. Include no-crash and repeated
pre-dispatch, post-commit and confirmation crash schedules.

For the two unchanged policies, reuse the previously verified, twice-identical
216-row results, checking their SHA-256 and matching case IDs and specifications.
Do not claim those historical rows are newly executed. Run both previous policies
on the 72 fresh cases. Run hybrid on all 288 cases. Every new episode uses fresh
databases, the same simulator, scorer, resource limits and requested fault/crash
schedule. Record which rows are reused and which are fresh. Prior timeouts remain
in the retained v2 attempt file; the reused result is the verified clean v2 report.

New cases are frozen before outcomes are inspected but share the same author and
simulator. They are not external validation or a blinded benchmark. Tests exercise
the five known regressions before the full run; disclose that these cases informed
the candidate rather than claiming they are held out.

## Predeclared checks

Each policy must satisfy the existing zero-duplicate, zero-wrong-effect,
zero-false-success, zero-budget-violation and zero-execution-error gates, and finish
all 24 completion controls. Hybrid must complete these five known loss cases:
`eval-v1-132`, `eval-v1-140`, `comparison-v2-005`, `comparison-v2-063`,
`comparison-v2-069`.

The no-regression goal additionally requires hybrid to lose no previously verified
completion relative to either policy across the 288 cases. Report each failing
case and keep a nonzero exit code if this goal is not met. Safety and completion
are separate: a safe result alone does not meet the no-regression goal. Do not
silently fix or tune frozen code after inspecting full results. Further fixes need
a new version. Even passing these checks is not automatic production promotion.

Report paired gains, losses, ties, reserved calls and virtual time, separating
cases with and without requested crashes. Actual crashes can differ because
policies traverse different hooks. Retain that path dependence explicitly.

## Reproduction

Freeze the protocol, candidate, runner, tests, case file, previous lock and clean
previous result. Previous locks are checked transitively. Commit before the full
run. Run `python3 -B comparison3.py > comparison-v3-results.json`. Progress goes to
stderr; the result JSON is deterministic. `--freeze` refuses overwrite. Repeat the
comparison unchanged and check identical output; preserve any failed attempts.

Assumptions remain one controller, truthful contracts, permanent idempotency keys,
an intact client journal, virtual time, and process exits rather than power loss.
No concurrency, key expiry, disk-loss recovery, real network latency, untrusted
controller isolation or production reliability estimates. No models, network calls,
real customer data or paid services.
