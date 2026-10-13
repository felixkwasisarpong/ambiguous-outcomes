# Frozen evaluation v1

This protocol is frozen before running the full evaluation. It evaluates the
existing deterministic controller without changing its implementation. It is a
separately specified synthetic test set, not an externally authored or blinded
benchmark: the same author knows the controller and uses the same simulator.
The scorer is independently implemented and does not call the development scorer.

## Construction

Seed: `20261013`. There are 144 cases:

- 24 completion controls: three supported recovery contracts, four fault profiles,
  and either no crash or two crashes inside the confirmation transaction. Each has
  an exclusive virtual deadline of 128 and a persistent budget of 32 tool calls.
- 120 stress cases: six contract families crossed with five fault modes and four
  budgets (1, 3, 7, 12). A seeded generator assigns delays, deadlines and crash
  schedules without consulting controller outcomes. Completion is not required
  under inadequate resources or contracts that cannot establish the outcome.

Fault modes include clean execution, lost replies, delayed commits, stale reads
and dropped requests. The maximum generated commit and visibility delays are 8
and 7 respectively. Truthful bounds vary with each case. Contract families include
idempotency with and without lookup, lookup without idempotency, no usable recovery
mechanism, and unknown settlement bounds. Keys do not expire.

Crash requests target fixed process boundaries. A hook that is not reached is
recorded as unreached, never silently counted as a crash. A normal return ends the
episode; the runner does not keep restarting to force an unreachable hook. Cases
that request two crashes do not necessarily experience two. Actual counts are
reported separately. Every case gets fresh databases, never reused from training
or development. The final worker uses the remaining original budget and deadline.

## Acceptance rules set before execution

All 144 cases must have zero excess effects, wrong effects, false success reports,
budget violations and execution errors. All 24 completion controls must reach
verified completion. Stress-case completion is descriptive, without a target.

A verified completion requires a success report naming the sole matching
committed effect, with no wrong effect. The evaluator drains delayed effects after
the controller stops. A late duplicate invalidates success. An unresolved report
never becomes verified completion just because a hidden action committed later.

Record statuses, reservations, committed service-call counters, actual process
crashes, unreached crash requests, and per-case ledgers. The service-call counter
can roll back with an interrupted service transaction; it is not a count of all
attempted calls. Client reservations are the authoritative budget measure.

Any failed gate is retained and reported. Do not tune the controller or change
cases to make this version pass. Follow-up fixes need a distinct version and an
explicit comparison with the frozen baseline. A passing result means only that
these rules passed in this synthetic evaluation. No population confidence
intervals, production success rates or novelty claims are warranted.

## Freeze and reproduction

`python3 -B evaluation.py --freeze` creates the specification and a SHA-256 lock
covering it, this protocol, the runner, its tests, and the baseline implementation.
It refuses to overwrite an existing version. Commit the frozen inputs before the
first full run. Run `python3 -B evaluation.py > evaluation-results.json`; a failed
acceptance gate produces a nonzero exit code while retaining the JSON report.
The runner checks the lock before launching any controller process. The report
records the lock hash and all frozen source hashes. Re-running with unchanged
inputs should produce identical JSON. This is a local audit trail, not a registry
preregistration or external timestamp authority.

The unit tests exercise the evaluation machinery using a few integration controls
before the full evaluation; this is disclosed, not a blind holdout claim. Once
results are inspected, v1 is a regression set rather than an untouched holdout.

## Exclusions and future model comparison

No network calls, model calls or real data. Process exits are not power failures.
One controller and one writer operate per case. No concurrency, dishonest
contracts, key expiry, corrupted-disk recovery, wall-clock downtime or realistic
network transit. The evaluator can inspect hidden state, but the controller only
receives public tools and its client journal. This is logical API separation,
not isolation against malicious controllers.

Before a paid model comparison, freeze a new evaluation version, define a common
tool interface and token/call budget, choose model settings, and obtain a spending
limit. Models must not see fault labels, future effects, oracle state or expected
outcomes. Record completion alongside false success, duplicate effects, latency
and cost. V1 evaluates only the existing deterministic baseline.
