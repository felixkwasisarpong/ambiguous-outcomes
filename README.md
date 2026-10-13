# Ambiguous outcomes

A deterministic Python simulator for studying recovery after a tool request times
out. A timeout can mean the request never committed, or that it committed but its
reply was lost. Retrying without enough evidence can create duplicate effects.

The examples use fictional service tickets, Python's standard library, and a
virtual clock. They make no network requests or model calls.

## Run

Requires Python 3.10 or later:

```sh
python3 -B -m unittest -v
python3 -B smoke.py > results.json
python3 -B delayed.py > delayed-results.json
python3 -B durable.py > durable-results.json
python3 -B evaluation.py > evaluation-results.json
python3 -B comparison.py > comparison-results.json
python3 -B comparison3.py > comparison-v3-results.json
```

## Experiments

`smoke.py` compares blind retries, same-key retries under a declared idempotency
contract, and stopping when the service offers neither idempotency nor status
lookup. Four fixtures demonstrate duplicate effects, safe same-key recovery, and
two hidden outcomes that produce identical visible timeout observations.

`delayed.py` adds delayed commits, stale reads, bounded reconciliation and tool
budgets. Its six fixtures demonstrate:

- An early negative lookup followed by a retry can cause a later duplicate.
- Waiting through a truthful settlement bound can establish completion.
- A deadline can expire before a pending action commits.
- Confirmed absence after the bound can permit a retry.
- An unknown settlement bound leaves the outcome unresolved.

An independent evaluator checks the committed-effects ledger, including effects
that arrive after the controller stops. Reports contain source hashes, visible
observations and separate evaluator results. Tests cover tenant scoping, payload
mismatch, authorization, budgets, deadlines and deterministic repeatability.

## Scope and limitations

The suite currently has 137 unit tests. These are hand-designed simulator checks,
not a representative production benchmark or empirical comparison of AI models.
There is no claim of a new method or production reliability.

The simulators assume truthful service contracts and do not model concurrent
writers or untrusted controllers. The original experiments use permanent keys;
the finite-retention extension below explicitly models key expiry. The original
smoke and delayed fixtures are in-memory; the durable extension uses separate
SQLite databases and sequential child processes. The API boundary is logical, not a security sandbox. The delayed
controller assumes one writer and initially empty task state. An unresolved
outcome is not counted as verified completion even if an action actually committed.

## Crash/restart extension

`durable.py` stores the client operation journal and the simulated service state
in separate SQLite databases. Each interrupted attempt and recovery runs in a
fresh Python process. Fault injection uses `os._exit(73)` at nine boundaries,
including inside an uncommitted service transaction and an uncommitted client
confirmation transaction. Temporary databases are removed after each fixture.

The journal saves the operation identity, request, declared contract, absolute
virtual deadline and call budget before dispatch. Recovery reuses the same key
when the service supports idempotency. Otherwise, it uses exact-request lookup
and a declared settlement bound before retrying. Unknown outcomes remain
unresolved when the contract cannot justify a retry. Reserved calls survive a
restart; a crash may waste a reservation but cannot reset the budget.

The 14 saved crash fixtures produce 13 verified completions and one unresolved
outcome under an unknown-bound, non-idempotent contract. No duplicate effect or
false success occurs in these fixtures. The unresolved case later commits one
effect, which the independent evaluator records without counting it as verified
completion. These counts describe only these fixtures, not expected success rates.

Additional tests deliberately forget the journal or change the operation identity;
both produce duplicate effects and false success, detected by the evaluator.
Tests also cover request/contract mismatches, persistent budgets and deadlines,
unauthorized tenants, corrupt journals, pending-key deduplication and rollback.

### Durability assumptions

This validates abrupt **process exits**, not power loss, filesystem corruption
recovery, distributed failover or simultaneous controllers. SQLite uses
`synchronous=FULL`; that setting alone is not evidence of hardware durability.
The service's idempotency record and effect commit atomically in this simulator.
Keys do not expire, identities must be unique per tenant, and exact lookup assumes
initially empty task state and one writer. Deleting a journal is not crash recovery.
The implementation does not detect an intentionally missing journal after use.

Virtual time persists in the service and advances only through explicit waits or
evaluator draining. It does not model time passing during real downtime or network
transit. The deadline is exclusive for starting a tool call; a result previously
confirmed in the journal can be returned after the deadline. Budgets count public
create, lookup and wait calls, not local clock or journal reads. API isolation is
logical; this is a research harness, not a production workflow engine.

## Frozen evaluation v1

The [protocol](EVALUATION.md), [144 cases](evaluation-v1.json), independent scorer
and [source lock](evaluation-v1.lock.json) were committed in `f444f99` before the
first full baseline execution. The baseline implementation was unchanged. The
cases are separate from the development fixtures but share the same simulator
and author; this is not an externally authored or blinded benchmark.

The [recorded results](evaluation-results.json) passed the predefined gates:

- Completion controls: **24 of 24** verified completions.
- Stress cases: **35 of 120** verified completions, 71 budget/deadline stops and
  14 unresolved outcomes.
- All 144 cases: **zero** excess effects, wrong effects, false success reports,
  budget violations or execution errors.
- **123 actual process crashes**, with two crashes reached in 41 cases. Another
  66 requested crash points were not reached and are reported separately.

A second complete run produced byte-for-byte identical JSON. All 58 unit tests
also passed. This establishes reproducibility of these synthetic cases, not a
production success rate. Passing safety gates does not establish that every
unresolved case was unavoidable or that the controller is optimal. Resource use
and missed completions remain useful targets for comparison.

The runner refuses altered frozen inputs and returns a failing exit code if an
acceptance gate fails. Keep v1 intact; future controller changes require a new
version for comparison. Now that v1 has been inspected, it is a regression set,
not an untouched holdout. No model calls or external API calls were made.

## Paired deterministic comparison v2

The [comparison protocol](COMPARISON.md), candidate policy, runner and
[216 cases](comparison-v2.json) were frozen in `a0b7236` before the first full
paired run. All v1 implementation and specification files remain unchanged.

The investigation classified the 85 original incomplete cases by observed stop:
38 reached their deadline, 35 used their call budget, and 12 were policy
abstentions under the specified precedence. The report retains overlapping flags;
these categories do not establish that completion was impossible.

The candidate in `strategies.py` tries a safe same-key replay before waiting when
the contract provides episode-long idempotency. It retains the original submission
time across replays. Non-idempotent operations use the original controller.

The [clean paired results](comparison-results.json) show:

- Baseline: **79 verified completions**, **559 reserved calls**.
- Key-first candidate: **95 verified completions**, **497 reserved calls**.
- Paired outcomes: **21 gains, 5 losses, 190 ties**.
- Both completed all **24 completion controls** with zero excess/wrong effects,
  false success reports, budget violations or execution errors.
- In 61 cases with no requested crashes: **6 gains, no losses, 55 ties**.
- In 155 cases with requested crashes: **15 gains, 5 losses, 135 ties**.

Four regressions arise after repeated pre-dispatch crashes with unknown settlement
bounds: the candidate abstains after one timed-out replay despite remaining
recovery options. One regression arises because a replay reaches a service-commit
crash hook and consumes the remaining budget; the baseline uses lookup and never
reaches that hook. Thus identical requested schedules need not produce identical
actual crashes. The candidate is an experimental alternative, not a replacement.

The [first attempt](comparison-attempt-1.json) is retained: one baseline worker
hit the unchanged 15-second process timeout on `eval-v1-064`, causing the execution
and baseline-reproduction gates to fail. The unchanged rerun passed; only that
baseline row differed and all candidate rows matched. A further complete run
produced byte-for-byte identical JSON to the clean comparison. The successful comparison
exactly reproduces every original v1 baseline row. All **68 unit tests** pass.

These are simulator results with known, permanent idempotency guarantees. They
are not production rates, AI performance evidence or proof of novelty. The original
144 cases are regression cases; the 72 newly specified cases share the same author
and simulator. No models or external services were called.

## Hybrid recovery comparison v3

The [third protocol](COMPARISON_V3.md), [hybrid policy](hybrid.py), runner and
[288-case specification](comparison-v3.json) were frozen in `023e1dd` before the
full comparison. V1 and v2 remain unchanged. The five known regressions were
explicit development targets and passed focused tests before this freeze.

The [first full comparison](comparison-v3-results.json) meets the predeclared
no-regression goal on this test set. An early full repeat had execution
timeouts; the later runtime audit obtained two exact full repeats under unchanged
limits (see the runtime audit below):

- Baseline: **104 verified completions**, **767 reserved calls**.
- Key-first candidate: **125 verified completions**, **687 reserved calls**.
- Hybrid candidate: **131 verified completions**, **725 reserved calls**.
- Hybrid fixes **all five known regressions**, gains 27 completions over baseline
  and 6 over key-first, and loses **zero** completions relative to either.
- All three complete the 24 controls with zero excess/wrong effects, false success
  reports, budget violations or execution errors.
- Without requested crashes (93 cases), hybrid gains 11 over baseline and ties
  key-first on all completions. With requested crashes (195 cases), it gains 16
  over baseline and 6 over key-first. Actual crash counts remain path-dependent.

Hybrid uses **42 fewer calls than baseline**, but **38 more than key-first**. The
extra completions therefore have a resource tradeoff; hybrid is not claimed to
minimize calls. These are reserved tool calls, not measured API prices or tokens.

The comparison reuses the hash-verified historical 216-case results for each
unchanged policy and runs those policies on 72 fresh cases. Hybrid runs freshly
on all 288. This provenance is recorded in the report; historical rows are not
presented as new executions. All **78 unit tests** pass, including request/limit
mismatch rejection, immutable key/timestamp checks and bounded unknown-outcome
recovery. No model or external API calls were made.

The [second full attempt](comparison-v3-attempt-2.json) is retained unchanged. It
hit the fixed 15-second worker timeout on hybrid cases `eval-v1-058`,
`comparison-v3-065` and `comparison-v3-066`. Every other row matched the first run;
all five target fixes still passed. This attempt fails the execution-error gate
and reports a completion loss on the first timed-out case. Do not treat it as a
passing or identical full repeat.

[Targeted replays](comparison-v3-timeout-rechecks.json) of all three timeout cases
then matched the first-run rows exactly under unchanged code and limits. These
replays support the functional results but do not replace a successful full
reproducibility run. The historical cause of the intermittent timeouts remains unconfirmed. Later
full repeats passed, as documented below. The policy remains experimental; no
production promotion follows.


Hybrid leaves 157 cases without verified completion. The cases use fictional
service tickets, permanent keys, truthful contracts and a virtual clock. This is
evidence about the simulator and selected cases, not production reliability,
optimality or a publishable novelty claim. No existing policy was replaced.

## Runtime audit and clean full repeats

The [audit procedure](RUNTIME_AUDIT.md) and diagnostic code were committed in
`dad61c9` before collecting the diagnostic runs. Frozen policies, cases, scorer,
SQLite settings, virtual deadlines, budgets and the **15-second process timeout**
were unchanged. No new evaluation limit was introduced.

The [targeted probes](runtime-targeted.json) cover four historically affected cases
with three paired original/traced repetitions: **24 episodes, 48 worker launches**.
Every episode exactly matched its original successful result and none timed out.
The slowest launch was **139.10 ms**. Tracing separates first-observable Python
startup, imports, journal operations and service operations. Events are written
unbuffered and preserved on timeout; intentional crash exits remain distinct.

Two sequential full runs then passed all checks:

- [Full run 1](runtime-full-1.json): **741 launches, zero timeouts**, maximum wall
  time **3,684.87 ms**; [timings](runtime-full-1-timings.json).
- [Full run 2](runtime-full-2.json): **741 launches, zero timeouts**, maximum wall
  time **3,824.78 ms**; [timings](runtime-full-2-timings.json).
- Both complete behavioral reports match each other and the first successful v3
  report **byte for byte**. Timing reports are separate and naturally differ.
- The v3 provenance remains explicit: each full run freshly executes hybrid on
  all 288 cases and each previous policy on the 72 new cases, reusing 216 verified
  historical rows per previous policy.

The slowest full-run workers used about 402 ms and 312 ms of child CPU respectively,
versus 3.68 s and 3.82 s of elapsed time. This shows substantial non-CPU time in
those launches; it does **not** identify scheduling, I/O, suspension or another
specific historical cause. The original 15-second stalls were not reproduced.
No controller defect or root-cause fix is claimed from their absence.

The clean-repeat requirement is now met under the original limits. Earlier failed
attempts remain in the repository. These runs do not guarantee future freedom
from intermittent host stalls. Diagnostics provide evidence for any recurrence.
All **87 unit tests** pass, including partial-timeout trace preservation, immutable
output files, CPU/wall-clock separation and distinction between crashes/timeouts.
No external model or API calls were made.

## Finite retention and elapsed downtime

The [protocol](RETENTION.md), [232 cases](retention-v1.json), new service, guarded
policy and tests were frozen in `2c7f117` before full execution. Earlier sources
and results are unchanged. All cases are newly executed with an atomic JSON-file
service, shared virtual time, finite or permanent keys, request transit delays
and elapsed downtime between process exits. This is a separate implementation
by the same author, **not independent external validation** or real-provider data.

The experiment tests a missing assumption: retrying the same key is safe only
while the provider still guarantees deduplication. Expired keys can create new
effects even when an earlier action remains pending. `window_guard` checks the
conservative expiry bound before replay and uses exact readback where possible.
Negative readback never renews its replay permission in this version.

The [first result](retention-v1-results.json) and [full repeat](retention-v1-repeat.json)
match **byte for byte**. Each run freshly executes 232 cases for each policy;
together they contain **928 episodes and 1,796 worker launches**, with zero
execution errors under the unchanged 15-second timeout. All **114 unit tests**
pass, including 27 new boundary, crash-recovery and scoring checks.

- The unmodified legacy hybrid reports 228 successes, but ledger verification
  accepts only **169**. It creates **59 duplicate effects** and makes **59 false
  success claims** when run outside its permanent-key assumptions.
- The guarded policy has **168 verified completions**, **zero duplicates**,
  **zero false success claims**, zero wrong effects and zero budget violations.
- Both complete all 12 clean controls and all 72 permanent-key stress cases.
- Paired verified outcomes are **28 gains, 29 losses and 175 ties** for the guard.
  It uses **444 reserved calls**, versus 481 for the legacy policy.
- The guard's 64 noncompletions comprise 61 expired-window stops and three
  budget/deadline stops. In 33 noncompleted cases, exactly one effect eventually
  exists but was not verified by the policy; those are not counted as successes.
- Of its 29 paired completion losses, 28 involve a dropped initial request and
  one a pending request. The guard is deliberately conservative and does not
  establish that these losses were unavoidable.

The candidate passes its predeclared safety and control gates. The legacy
finite-key policy does **not** pass safety; its diagnostic failures remain fully
visible even though they are not an acceptance requirement for the guard. This
is an assumption-violation demonstration, not a comparison against a competing
TTL-aware method, and not a general superiority claim.

These results expose a safety/completion tradeoff. They do not establish novelty,
production reliability or measured AI performance. Provider contracts are truthful,
clocks are shared, lookup is exact, and there is one writer. Wall-clock drift,
concurrency, provider outages and changing contracts remain outside this model.
No model, paid service or external API calls were made.

To reproduce without replacing a saved attempt:

```sh
python3 -B retention_eval.py --output new-retention-attempt.json
```

## Settled-absence recovery

The [protocol](RECONCILIATION.md), [304-case manifest](reconciliation-v1.json), new
journal evidence, candidate and TTL-aware reference were frozen in `2b9d380`
before the first full run. The original 232 cases are disclosed regression data;
72 new stress cases cover non-tight transit bounds, unknown settlement bounds and
repeated crashes. All three policies are freshly executed; no old rows are reused.

The candidate persists both the replay epoch and latest possible dispatch time.
After expiry, a new attempt requires an exact negative read that starts after the
latest dispatch plus maximum transit plus the settlement/visibility bound. Budget
reservation and new dispatch evidence commit atomically before the call. Absence
proofs are not cached across crashes. The original key, identity, deadline and
budget remain unchanged. Without lookup, waiting is capped to allow a retry before
the conservative expiry boundary when possible.

The [first full result](reconciliation-v1-results.json) and
[clean-export repeat](reconciliation-v1-repeat.json) match **byte for byte**.
The repeat was executed from a Git archive of the frozen commit, excluding Git
metadata and untracked workspace files. Its [verification record](reconciliation-clean-export.json)
includes the source/archive hashes and Python version. All **137 tests** passed
both in the workspace and the clean export. This is same-author, same-host
reproduction, not independent external validation.

Across the two runs: **1,824 episodes**, **3,724 worker launches**, zero execution
errors, and an unchanged 15-second process timeout. Per complete 304-case run:

- Frozen conservative guard: **201 verified completions**, **693 reserved calls**.
- TTL-aware readback-first reference: **221 verified completions**, **866 calls**.
- Reconciled candidate: **229 verified completions**, **765 calls**.
- All three have zero duplicate or wrong effects, false success claims, budget
  violations or execution errors, and complete all 12 clean controls.
- On the original 232 cases, the candidate increases verified completions from
  **168 to 183**, fixes all **15 declared targets**, and loses no old completion.
  The frozen guard reproduces all 232 historical rows exactly.
- Across all 304 cases, the candidate gains **28** completions and loses **zero**
  relative to the old guard, at a cost of 72 additional reserved calls.
- Against readback-first it has **9 gains, 1 loss and 294 ties**, while using 101
  fewer reserved calls. This is not case-by-case dominance or a global optimum.

The loss to readback-first is `reconciliation-v1-028`. After repeated pre-dispatch
crashes, the candidate correctly creates after settled absence, but then waits
through another settlement window before attempting positive readback. Its
20-unit deadline is reached. The reference checks immediately and confirms the
already committed effect at time 18. This limitation remains in the frozen result;
the candidate is not retrospectively tuned to remove it.

The candidate still leaves **75** cases unverified: 44 expired-window stops, two
stops with no time-progressing safe replay, eight budget/deadline stops and 21
unknown-settlement stops. In 41 of these cases one effect eventually exists, but
it was not verified, so it is not counted as completion. Fourteen old dropped/
no-lookup losses are not promised recoverable. A focused test constructs identical
visible histories for dropped and committed-but-reply-lost outcomes after expiry;
a forced retry duplicates only the committed world. That is a narrow ambiguity
witness, not proof that all remaining cases are unavoidable.

The new service cases, policies and baseline share an author. Truthful bounded
contracts, exact lookup, one writer and a shared virtual clock remain assumptions.
These are synthetic results, not measured AI performance, a novelty finding or
production evidence. No external API or model calls were made.

## Independent review and next steps

The [independent-review packet](REVIEW.md) includes reproduction commands, safety
questions, known counterexamples and suggestions for an independently authored
service. No outside review has occurred, and no review request has been sent.
Reviewer selection and actual external validation remain pending.

Keep published versions frozen. Prioritize an outside assessment of the latest-
dispatch absence argument, baseline fairness and service assumptions. Any fix for
`reconciliation-v1-028` belongs in a new version. Systematic repeated faults,
concurrency, drift and real provider contracts still need separate evaluations.
Check relevant prior work before framing a paper's contribution. Paid model runs
still require an explicit spending limit.
