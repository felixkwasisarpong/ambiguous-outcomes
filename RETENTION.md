# Finite retention and elapsed downtime, version 1

## Question and declared scope

Do finite idempotency windows invalidate a retry policy designed for permanent
keys? Can an explicit replay-window guard avoid duplicates while preserving useful
verified completion? Characterize both safety and missed completions; no result
or aggregate superiority is assumed.

V1–v3 and their reports remain frozen. This is a new experiment with a separately
implemented JSON-snapshot service, not the old SQLite service. It has the same
author and reuses the durable client journal, API value types and independent
ledger scorer. It is NOT external validation, an actual provider benchmark, proof
of novelty, or evidence of production or AI-agent performance.

## Service contract

The single-writer service accepts one initially absent fictional ticket operation.
A key reserves its effect atomically and expires at acceptance time plus its TTL.
At exact expiry it is reusable. Duplicate requests before expiry do not extend the
window. Expiry removes deduplication state, not committed or pending effects.
A pending effect may still commit after its key expires. After expiry the same
key can create another effect. Permanent keys are included as controls.

A truthful public contract exposes TTL, exact-request lookup availability, a
settlement-and-visibility bound, and a maximum one-way request latency. Time uses
one shared virtual clock with no clock drift. Every create/lookup advances time
by actual request latency; explicit waits and injected process downtime also
advance it. This is elapsed synthetic time, not measured network latency or
wall-clock distributed time. There is no response transit delay. The settlement
bound starts at server acceptance. Successful replay responses can identify a
committed effect even while search visibility is stale.

Requests and lost replies affect only the first new create. Later creates use
normal behavior. No other writer, provider contract violation, expired status
record, authentication expiry, rate limit or nondeterministic clock is modeled.
Atomic file replacement exercises process termination only, not power-loss
recovery. Neither policy can access hidden fault labels, ledgers or evaluator
clock-advance methods; the boundary remains logical, not a security sandbox.

## Policies

1. **legacy_hybrid:** the exact frozen v3 policy, unmodified. It ignores the new TTL
   and transit bound and is knowingly run outside its permanent-key assumptions.
   Finite-key failures are assumption-violation diagnostics, not v3 regressions
   or evidence against a competing TTL-aware method.
2. **window_guard:** save the original key, request, limits and first dispatch
   reservation. Allow a retry only when current time plus maximum request latency
   is strictly earlier than first reservation plus TTL. The reservation precedes
   acceptance, making this expiry estimate conservative. Never refresh it on a
   replay or restart. Permanent keys bypass the TTL check. After ambiguity, use
   bounded waiting and exact readback when available. Positive readback can confirm
   after expiry. Negative readback never renews permission to write in this first
   policy; even some safely recoverable absent cases may be left unresolved.

All external create/lookup/wait calls reserve from the original persistent budget.
Starting a call at the absolute deadline is prohibited; a call started earlier
may finish after it. Downtime is an environmental event, not a tool call. Previously
confirmed journal results remain available after the deadline.

## Cases, development and freezing

The full deterministic grid comprises 216 stress cases: TTL permanent/3/6; first
outcome committed-and-reply-lost/pending(commit 3, visibility 2)/dropped; lookup
yes/no; latency 0/1; downtime 0/2/3/5/6/9. Stress cases terminate the first worker
at `after_dispatch` and then advance downtime before a fresh recovery process.
Settlement bound is 5, call budget 12, absolute deadline 20. Twelve clean response
controls cross TTL, lookup and latency without a crash. Four boundary cases cross
deadline 0/1 and budget 0/1. Total: **232 cases per policy**, all freshly executed.

Unit tests are development examples, including exact TTL boundaries, stale
readback, a dropped request, pending effects after expiry, transit crossing expiry,
expired deadlines, immutable keys and multiple crash positions. They are visible
before freezing; the full grid shares the same author and is not held out or
blinded. Freeze protocol, service, policy, runner, tests and case manifest, with
transitive v3 source verification, and commit BEFORE the first full run.

## Gates and reporting

The candidate must have zero excess effects, wrong effects, false success claims,
budget violations and execution errors, and complete all 12 clean controls.
Both policies must meet safety and applicable controls on permanent-key cases.
Execution errors and budget violations fail the experiment for either policy,
even on finite keys. The legacy finite-key safety results are fully reported but
are intentionally not a candidate acceptance requirement. No aggregate completion
or no-regression gate is imposed: conservative refusal is an explicit tradeoff.

Report all rows, both at-stop and eventual ledger scoring, requested and actual
crashes, reserved calls, paired verified completion gains/losses/ties, and every
candidate stop reason. An unverified existing effect is never counted as success.
The scorer is separate from policy and service code and does not trust their
success labels. Reuse no prior result rows. Run two complete comparisons
sequentially with the unchanged **15-second worker timeout**; preserve failures
and compare behavioral reports byte for byte. Do not tune the frozen candidate
or limits after observing full results. A fix requires a new version.

## Commands

```sh
python3 -B -m unittest -q
python3 -B retention_eval.py --freeze
# Commit the frozen sources before full execution.
python3 -B retention_eval.py --output retention-v1-results.json
python3 -B retention_eval.py --output retention-v1-repeat.json
cmp retention-v1-results.json retention-v1-repeat.json
```

Output files must be new; the runner refuses to overwrite an attempt. Only
synthetic data is used. No network, paid service or model calls are made. Any
later paper must retain these limitations and establish relevance and novelty
through literature review and external evaluation.
