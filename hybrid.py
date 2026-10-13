"""Third deterministic candidate: same-key retries plus bounded readback.

No fault labels, hidden service state or future outcomes are passed to this policy.
"""
from durable import Exhausted, recover as baseline
from smoke import Observation, Report


def hybrid(journal, tools, request, operation="synthetic-operation", deadline=12,
           max_calls=12, crash=lambda point: None):
    if not tools.contract.idempotent:
        return baseline(journal, tools, request, operation, deadline, max_calls, crash)
    observations = []

    def result(status, ticket=None):
        return Report(status, ticket, tuple(observations))

    def confirm(ticket):
        if ticket.request != request:
            return result("rejected")
        crash("before_confirmation")
        journal.confirm(operation, ticket)
        crash("after_confirmation")
        return result("success_observed", ticket.id)

    def dispatch(first=False):
        # Only a new operation sets the uncertainty-window origin. Replays keep
        # the exact request and key, original timestamp, deadline and total budget.
        journal.reserve(operation, tools.now(), dispatch=first)
        crash("before_dispatch")
        try:
            ticket = tools.create(request, row["op_key"])
        except TimeoutError:
            observations.append(Observation("timeout", "No response received"))
            ticket = None
        crash("after_dispatch")
        return ticket

    try:
        crash("before_prepare")
        row = journal.prepare(operation, request, tools.contract, deadline, max_calls)
        crash("after_prepare")
        if row["ticket"]:
            observations.append(Observation("journal", "Previously confirmed result"))
            return result("success_observed", row["ticket"])
        if row["submitted_at"] is None:
            ticket = dispatch(first=True)
            if ticket:
                return confirm(ticket)
        # Preserve the useful immediate replay from the key-first candidate.
        ticket = dispatch()
        if ticket:
            return confirm(ticket)
        while True:
            row = journal.get(operation)
            bound = tools.contract.settlement_bound
            if bound is not None and tools.now() < row["submitted_at"] + bound:
                journal.reserve(operation, tools.now())
                tools.wait_until(min(row["submitted_at"] + bound, deadline))
                observations.append(Observation("wait", str(tools.now())))
            # Readback can avoid another write and its crash boundary. Missing
            # readback never authorizes an opaque retry: this branch is keyed.
            if tools.contract.lookup:
                journal.reserve(operation, tools.now())
                found = tools.find(request)
                observations.append(Observation("lookup", ",".join(t.id for t in found) or "missing"))
                if len(found) == 1:
                    return confirm(found[0])
                if len(found) > 1:
                    return result("duplicate_detected")
            # With an unknown bound, same-key replays remain safe. The original
            # persistent budget bounds repeated attempts, including across exits.
            ticket = dispatch()
            if ticket:
                return confirm(ticket)
    except Exhausted:
        return result("budget_or_deadline_exhausted")
    except (ValueError, PermissionError):
        return result("rejected")
