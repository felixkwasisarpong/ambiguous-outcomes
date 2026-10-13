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

The suite currently has 58 unit tests. These are hand-designed simulator checks,
not a representative production benchmark or empirical comparison of AI models.
There is no claim of a new method or production reliability.

The simulators assume truthful service contracts. They do not model concurrent
writers, key expiration or untrusted controllers. The original smoke and delayed
fixtures are in-memory; the durable extension uses separate SQLite databases and
sequential child processes. The API boundary is logical, not a security sandbox. The delayed
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

## Next steps

Analyze resource use and unresolved outcomes, then specify a new comparison
version before changing policies. Compare deterministic alternatives before
claiming that model-assisted recovery offers value. A later model experiment
needs a common tool interface, a fixed evaluation protocol and an explicit
spending limit. No model has been evaluated yet.
