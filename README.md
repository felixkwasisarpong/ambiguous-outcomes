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

The suite currently has 23 unit tests. These are hand-designed simulator checks,
not a representative production benchmark or empirical comparison of AI models.
There is no claim of a new method or production reliability.

The simulator is single-process and assumes truthful service contracts. It does
not model concurrent writers, crash persistence, key expiration or untrusted
controllers. The API boundary is logical, not a security sandbox. The delayed
controller assumes one writer and initially empty task state. An unresolved
outcome is not counted as verified completion even if an action actually committed.

## Next steps

Add a durable operation journal and crash/restart tests, then integrate idempotency
and reconciliation. Freeze independent evaluation fixtures before comparing
model-based recovery with the deterministic baseline.
