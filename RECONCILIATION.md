# Settled-absence recovery, version 1

## Question and development targets

Can bounded negative readback recover completions lost by the conservative
finite-key guard while preserving safety? Add a separately specified TTL-aware
reference instead of comparing only against a permanent-key assumption violation.
All earlier protocols, source locks and reports remain immutable.

The prior result informed this policy. Fifteen disclosed development targets are
14 dropped-request cases with lookup, plus pending/no-lookup `retention-v1-187`.
Their exact IDs are recorded in the runner and manifest. Fourteen other prior
losses involve dropped requests without lookup after the conservative key window.
Do not infer their hidden dropped state from the test labels or blindly retry them.
A dropped request and a lost reply after commit can have identical visible
histories in that situation. The remaining losses are not promised recoverable.

## Evidence and candidate

Persist a current replay epoch and the latest possible dispatch timestamp beside
the unchanged operation identity, key, original submission time, deadline and
budget. Reserving a create call, starting a fresh epoch if allowed, and advancing
the latest-dispatch record occur in one SQLite transaction BEFORE dispatch.
A crash may waste a reservation, but cannot erase dispatch uncertainty or create
another allowance. Strategy identity is persistent; missing dispatch history from
another journal format is rejected rather than guessed.

`reconciled_guard` first attempts same-key replay only while current time plus
maximum request latency is strictly before epoch start plus TTL. When lookup is
available, it waits through LATEST dispatch plus maximum transit plus the truthful
settlement/visibility bound, then issues exact-request lookup. One result confirms;
multiple results stop as a detected duplicate. A negative read that STARTED at
or after that bound establishes absence in this single-writer contract. Only
then can a new epoch authorize another create. The original key and limits remain
unchanged. Unknown settlement bounds never authorize writes based on absence.

The negative proof is not cached. An exit after readback but before dispatch
reservation requires another read. After reservation, recovery treats the new
attempt as possibly sent, including when termination occurred before the actual
call. Later readback must cover that attempt as well. Tracking only the initial
submission would incorrectly ignore later potentially accepted retries.

Without lookup, no absence-based renewal is available. Wait only as far as a
safe final send time (epoch plus TTL minus maximum transit minus one integer time
unit), allowing an earlier replay before expiry rather than waiting past it.
If no time-progressing safe retry remains, stop instead of spending calls on
identical observations. This rule uses integer virtual time, not real distributed clocks. Permanent keys
have no TTL cutoff. Budgets and exclusive call-start deadlines remain persistent.

## Reference policies

- `window_guard`: exactly the frozen conservative finite-retention policy.
- `readback_first`: a separately implemented decision loop using the same durable
  evidence primitives. Prefer readback immediately after ambiguity. When missing,
  wait through the latest settlement bound and reread; only settled absence
  authorizes writing. With no lookup, use safe same-key retries and unit polling.
  Unknown settlement with negative lookup stops unresolved. This is a simple
  same-author TTL-aware reference, not an externally authored published method.
- `reconciled_guard`: candidate described above.

No policy sees faults, future effects, other outcomes or evaluator state. Service
and scorer remain unchanged from finite-retention v1. Atomic JSON service state,
truthful bounds, exact lookup, one writer and a shared virtual clock remain strong
assumptions. No concurrency, real provider, power-loss or model evaluation occurs.

## Frozen evaluation and checks

Freshly execute all three policies on the old 232 cases and 72 new stress cases:
TTL 2/5, known/unknown settlement, actual latency 0/1, first reply-lost/pending/
dropped, and three crash schedules. All new cases offer lookup; the max transit
bound exceeds actual transit by one, downtime is two, deadline 20, budget 12.
Pending commits take two units plus two visibility units. Known bounds are five.
Schedules are two pre-dispatch exits, after-dispatch then after-absence exits,
and two confirmation-transaction exits. Unreached crash requests are reported.
New cases share the author and service; they are not external or blinded holdout.

Before any full run: run development tests; freeze source, protocol, cases, tests,
prior lock and prior result; commit. Development tests explicitly cover the 15
targets, later dispatch versus stale absence, unknown bounds, crash boundaries,
immutable limits and observationally indistinguishable outcomes.

Predeclared gates: all policies pass zero duplicate/wrong effect/false success/
budget violation/execution error gates and all 12 clean controls; old guard
reproduces all 232 historical rows exactly; candidate completes all 15 targets;
candidate loses no old guard completion on the original 232 cases. Report every
paired loss on the full set and against readback-first even when these gates pass.
No dominance over the new reference or new-case no-regression is assumed.

Use fresh state for every episode and reuse no result rows. Run two full
comparisons sequentially, keep the 15-second worker timeout, preserve unsuccessful
attempts and compare the full behavioral JSON byte for byte. Never tune frozen
code or overwrite a failure. Changed policies or limits require another version.

```sh
python3 -B -m unittest -q
python3 -B reconciliation_eval.py --freeze
# Commit frozen sources before the following commands.
python3 -B reconciliation_eval.py --output reconciliation-v1-results.json
python3 -B reconciliation_eval.py --output reconciliation-v1-repeat.json
cmp reconciliation-v1-results.json reconciliation-v1-repeat.json
```

No paid services, model calls, private documents or customer data are used.
Passing this comparison is not external validation, a novelty claim or production
promotion. A review packet will expose assumptions and counterexamples for a
separate person to assess; preparing that packet is not evidence of their review.
