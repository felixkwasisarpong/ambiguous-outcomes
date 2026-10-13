"""Separately specified readback-first TTL-aware reference, same author.

Shares storage primitives, not the candidate's decision loop. It is not an
independently authored baseline or a claim about a published algorithm.
"""
from durable import Exhausted
from smoke import Observation, Report


def readback_first(journal, tools, request, operation='synthetic-operation', deadline=20,
                   max_calls=12, crash=lambda point: None):
    observations = []
    c = tools.contract

    def result(status, ticket=None):
        return Report(status, ticket, tuple(observations))

    def save(ticket):
        if ticket.request != request:
            return result('rejected')
        crash('before_confirmation')
        journal.confirm(operation, ticket)
        crash('after_confirmation')
        return result('success_observed', ticket.id)

    def send(fresh):
        journal.reserve_create(operation, tools.now(), new_epoch=fresh)
        crash('before_dispatch')
        try:
            ticket = tools.create(request, row['op_key'])
        except TimeoutError:
            observations.append(Observation('timeout', 'No response received'))
            ticket = None
        crash('after_dispatch')
        return ticket

    def wait(until):
        journal.reserve(operation, tools.now())
        tools.wait_until(min(until, deadline))
        observations.append(Observation('wait', str(tools.now())))

    try:
        crash('before_prepare')
        row = journal.prepare_strategy(operation, request, c, deadline, max_calls, 'readback_first')
        crash('after_prepare')
        if row['ticket']:
            observations.append(Observation('journal', 'Previously confirmed result'))
            return result('success_observed', row['ticket'])
        if row['submitted_at'] is None:
            ticket = send(True)
            if ticket:
                return save(ticket)
        while True:
            evidence = journal.window(operation)
            if c.lookup:
                bound = None if c.settlement_bound is None else evidence['last_dispatch_at'] + c.max_request_latency + c.settlement_bound
                journal.reserve(operation, tools.now())
                started = tools.now()
                found = tools.find(request)
                observations.append(Observation('lookup', ','.join(t.id for t in found) or 'missing'))
                if len(found) == 1:
                    return save(found[0])
                if len(found) > 1:
                    return result('duplicate_detected')
                if bound is None:
                    return result('unresolved_no_settlement_bound')
                if started < bound:
                    if tools.now() < bound:
                        wait(bound)
                    continue
                observations.append(Observation('settled_absence', str(started)))
                crash('after_absence')
                ticket = send(True)
                if ticket:
                    return save(ticket)
            else:
                if c.key_ttl is not None and tools.now() + c.max_request_latency >= evidence['epoch_at'] + c.key_ttl:
                    return result('retry_window_expired')
                ticket = send(False)
                if ticket:
                    return save(ticket)
                # Simple polling baseline, deliberately independent of the
                # candidate's bound/last-safe-start scheduling heuristic.
                wait(tools.now() + 1)
    except Exhausted:
        return result('budget_or_deadline_exhausted')
    except (ValueError, PermissionError):
        return result('rejected')
