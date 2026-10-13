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

The suite currently has 48 unit tests. These are hand-designed simulator checks,
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

## Next steps

Freeze an independent evaluation set beyond the development fixtures, including
repeated interruptions, weaker contracts and more varied delays. Define metrics
and acceptance criteria before comparing a model-assisted controller with this
deterministic reference implementation. No model has been evaluated yet.
