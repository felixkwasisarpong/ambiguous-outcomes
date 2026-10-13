"""Candidate policy; frozen baseline durable.py is intentionally unchanged."""
from durable import Exhausted, recover as baseline
from smoke import Observation, Report


def key_first(journal, tools, request, operation="synthetic-operation", deadline=12,
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
        # A replay must not reset the original uncertainty window.
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
        # One immediate safe replay before waiting. Works even when lookup is
        # stale or unavailable, provided the episode-long key contract holds.
        ticket = dispatch()
        if ticket:
            return confirm(ticket)
        row = journal.get(operation)
        bound = tools.contract.settlement_bound
        if bound is None:
            return result("unresolved")
        settled_at = row["submitted_at"] + bound
        if tools.now() < settled_at:
            journal.reserve(operation, tools.now())
            tools.wait_until(min(settled_at, deadline))
            observations.append(Observation("wait", str(tools.now())))
        ticket = dispatch()
        return confirm(ticket) if ticket else result("unresolved")
    except Exhausted:
        return result("budget_or_deadline_exhausted")
    except (ValueError, PermissionError):
        return result("rejected")
