"""Replay within a conservative key window; reopen only on settled absence."""
from durable import Exhausted
from smoke import Observation, Report


def reconciled_guard(journal, tools, request, operation='synthetic-operation', deadline=20,
                     max_calls=12, crash=lambda point: None):
    observations = []
    contract = tools.contract

    def result(status, ticket=None):
        return Report(status, ticket, tuple(observations))

    def confirm(ticket):
        if ticket.request != request:
            return result('rejected')
        crash('before_confirmation')
        journal.confirm(operation, ticket)
        crash('after_confirmation')
        return result('success_observed', ticket.id)

    def safe_replay():
        origin = journal.window(operation)['epoch_at']
        return contract.key_ttl is None or tools.now() + contract.max_request_latency < origin + contract.key_ttl

    def settlement():
        last = journal.window(operation)['last_dispatch_at']
        return None if contract.settlement_bound is None else last + contract.max_request_latency + contract.settlement_bound

    def dispatch(new_epoch=False):
        if not new_epoch and not safe_replay():
            raise ValueError('Unsafe replay attempted')
        journal.reserve_create(operation, tools.now(), new_epoch=new_epoch)
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
        row = journal.prepare_strategy(operation, request, contract, deadline, max_calls, 'reconciled_guard')
        crash('after_prepare')
        if row['ticket']:
            observations.append(Observation('journal', 'Previously confirmed result'))
            return result('success_observed', row['ticket'])
        if row['submitted_at'] is None:
            ticket = dispatch(new_epoch=True)
            if ticket:
                return confirm(ticket)
        while True:
            if safe_replay():
                ticket = dispatch()
                if ticket:
                    return confirm(ticket)
            elif not contract.lookup:
                return result('retry_window_expired')

            bound = settlement()
            if contract.lookup:
                if bound is not None and tools.now() < bound:
                    wait(bound)
                journal.reserve(operation, tools.now())
                # The read must START after every possible dispatch has settled.
                read_started = tools.now()
                found = tools.find(request)
                observations.append(Observation('lookup', ','.join(t.id for t in found) or 'missing'))
                if len(found) == 1:
                    return confirm(found[0])
                if len(found) > 1:
                    return result('duplicate_detected')
                if bound is not None and read_started >= bound:
                    observations.append(Observation('settled_absence', str(read_started)))
                    crash('after_absence')
                    # No proof cache: a crash before reservation repeats readback.
                    # Reservation atomically consumes this permission before send.
                    ticket = dispatch(new_epoch=True)
                    if ticket:
                        return confirm(ticket)
                    continue
                if not safe_replay():
                    return result('unresolved_no_settlement_bound')
            else:
                if not safe_replay():
                    return result('retry_window_expired')
                # Do not wait past the final safe send time. Integer virtual time
                # is part of this model; this is not a wall-clock scheduling rule.
                until = bound if bound is not None else tools.now() + 1
                if contract.key_ttl is not None:
                    last_safe = journal.window(operation)['epoch_at'] + contract.key_ttl - contract.max_request_latency - 1
                    until = min(until, last_safe)
                if until > tools.now():
                    wait(until)
                else:
                    return result('unresolved_before_expiry')
    except Exhausted:
        return result('budget_or_deadline_exhausted')
    except (ValueError, PermissionError):
        return result('rejected')
