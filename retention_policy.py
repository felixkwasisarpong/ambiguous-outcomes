"""Conservative finite-retention recovery; no hidden service state is consulted."""
from durable import Exhausted
from smoke import Observation, Report


def window_guard(journal, tools, request, operation='synthetic-operation', deadline=20,
                 max_calls=12, crash=lambda point: None):
    observations = []

    def result(status, ticket=None):
        return Report(status, ticket, tuple(observations))

    def confirm(ticket):
        if ticket.request != request:
            return result('rejected')
        crash('before_confirmation')
        journal.confirm(operation, ticket)
        crash('after_confirmation')
        return result('success_observed', ticket.id)

    def replay_safe():
        ttl = tools.contract.key_ttl
        origin = journal.get(operation)['submitted_at']
        # Earliest possible key expiry, allowing the maximum request transit.
        # The first reservation may precede acceptance, making this conservative.
        return ttl is None or tools.now() + tools.contract.max_request_latency < origin + ttl

    def dispatch(first=False):
        if not first and not replay_safe():
            raise ValueError('Internal unsafe replay attempt')
        journal.reserve(operation, tools.now(), dispatch=first)
        crash('before_dispatch')
        try:
            ticket = tools.create(request, row['op_key'])
        except TimeoutError:
            observations.append(Observation('timeout', 'No response received'))
            ticket = None
        crash('after_dispatch')
        return ticket

    try:
        crash('before_prepare')
        row = journal.prepare(operation, request, tools.contract, deadline, max_calls)
        crash('after_prepare')
        if row['ticket']:
            observations.append(Observation('journal', 'Previously confirmed result'))
            return result('success_observed', row['ticket'])
        if row['submitted_at'] is None:
            ticket = dispatch(first=True)
            if ticket:
                return confirm(ticket)
        while True:
            if replay_safe():
                ticket = dispatch()
                if ticket:
                    return confirm(ticket)
            elif not tools.contract.lookup:
                return result('retry_window_expired')
            bound = tools.contract.settlement_bound
            origin = journal.get(operation)['submitted_at']
            settled = None if bound is None else origin + tools.contract.max_request_latency + bound
            if settled is not None and tools.now() < settled:
                journal.reserve(operation, tools.now())
                tools.wait_until(min(settled, deadline))
                observations.append(Observation('wait', str(tools.now())))
            if tools.contract.lookup:
                journal.reserve(operation, tools.now())
                found = tools.find(request)
                observations.append(Observation('lookup', ','.join(t.id for t in found) or 'missing'))
                if len(found) == 1:
                    return confirm(found[0])
                if len(found) > 1:
                    return result('duplicate_detected')
            if not replay_safe():
                # Negative readback does not renew this policy's replay window.
                # Even a provable absence is left unresolved in this first version.
                return result('retry_window_expired')
    except Exhausted:
        return result('budget_or_deadline_exhausted')
    except (ValueError, PermissionError):
        return result('rejected')
