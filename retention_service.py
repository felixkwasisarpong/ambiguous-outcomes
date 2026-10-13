"""Separately implemented finite-key service; same author, synthetic virtual time.

Atomic JSON snapshots replace the old SQLite service implementation. This is a
single-writer process-crash model, not an independent validation or power-loss test.
"""
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path

from durable import Fault, Tools
from smoke import Request, Snapshot, SYNTHETIC_REQUEST, Ticket


@dataclass(frozen=True)
class RetentionContract:
    idempotent: bool = True
    lookup: bool = True
    settlement_bound: int | None = 5
    key_ttl: int | None = 6
    max_request_latency: int = 0


class RetentionService:
    def __init__(self, path, crash=lambda point: None):
        self.path = Path(path)
        self.state = json.loads(self.path.read_text())
        self.contract = RetentionContract(**self.state['contract'])
        self.crash = crash

    @classmethod
    def initialize(cls, path, contract=RetentionContract(), fault=Fault(), latency=0):
        if not contract.idempotent:
            raise ValueError('This experiment requires keyed operations')
        if contract.key_ttl is not None and (type(contract.key_ttl) is not int or contract.key_ttl <= 0):
            raise ValueError('Retention must be positive or permanent')
        if (type(latency) is not int or latency < 0 or
                type(contract.max_request_latency) is not int or contract.max_request_latency < latency):
            raise ValueError('Latency exceeds declared bound')
        if min(fault.commit_delay, fault.visibility_delay) < 0:
            raise ValueError('Negative delay')
        if contract.settlement_bound is not None:
            if contract.settlement_bound < 0 or (not fault.drop_request and
                    fault.commit_delay + fault.visibility_delay > contract.settlement_bound):
                raise ValueError('Untruthful settlement bound')
        state = {'contract': asdict(contract), 'fault': asdict(fault), 'latency': latency,
                 'now': 0, 'calls': 0, 'first_used': False, 'actions': [], 'keys': {},
                 'tenant': SYNTHETIC_REQUEST.tenant}
        with Path(path).open('x') as handle:
            json.dump(state, handle, sort_keys=True)

    def close(self):
        pass

    def _save(self, effect_transaction=False):
        # An exit before replace leaves the previous complete snapshot intact.
        pending = self.path.with_suffix('.pending')
        with pending.open('w') as handle:
            json.dump(self.state, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        if effect_transaction:
            self.crash('during_service_commit')
        os.replace(pending, self.path)
        if effect_transaction:
            self.crash('after_service_commit')

    def now(self):
        return self.state['now']

    def tools(self):
        return Tools(self.contract, self.now, self.create, self.find, self.wait_until)

    def _advance(self, until):
        if until < self.now():
            raise ValueError('Clock cannot move backwards')
        self.state['now'] = until
        for action in self.state['actions']:
            if action['due'] <= until:
                action['committed'] = True
        self.state['keys'] = {k: v for k, v in self.state['keys'].items()
                              if v['expires'] is None or until < v['expires']}

    def elapse(self, duration):
        """Harness-only downtime; not a controller tool or a reserved API call."""
        if duration < 0:
            raise ValueError('Negative downtime')
        self._advance(self.now() + duration)
        self._save()

    def _validate(self, request):
        if request.tenant != self.state['tenant']:
            raise PermissionError('Tenant not authorized')
        if not all((request.tenant.strip(), request.intent.strip(), request.subject.strip())):
            raise ValueError('Empty request')

    @staticmethod
    def _ticket(action):
        return Ticket(action['id'], Request(**action['request']))

    def create(self, request, key):
        self._validate(request)
        if not key:
            raise ValueError('Key required')
        self.state['calls'] += 1
        self._advance(self.now() + self.state['latency'])
        scope = json.dumps([request.tenant, key])
        saved = self.state['keys'].get(scope)
        lost = False
        action = None
        if saved:
            action = self.state['actions'][saved['index']]
            if action['request'] != asdict(request):
                raise ValueError('Key payload mismatch')
        else:
            fault = Fault(**self.state['fault']) if not self.state['first_used'] else Fault(lose_reply=False)
            self.state['first_used'] = True
            lost = fault.lose_reply or fault.drop_request
            if not fault.drop_request:
                due = self.now() + fault.commit_delay
                action = {'id': f"ticket-{len(self.state['actions']) + 1}", 'request': asdict(request),
                          'due': due, 'visible': due + fault.visibility_delay,
                          'committed': fault.commit_delay == 0}
                self.state['keys'][scope] = {
                    'index': len(self.state['actions']),
                    'expires': None if self.contract.key_ttl is None else self.now() + self.contract.key_ttl}
                self.state['actions'].append(action)
        self._save(effect_transaction=bool(action and action['committed']))
        if lost or action is None or not action['committed']:
            raise TimeoutError('No response received')
        return self._ticket(action)

    def find(self, request):
        self._validate(request)
        if not self.contract.lookup:
            raise ValueError('Lookup unsupported')
        self.state['calls'] += 1
        self._advance(self.now() + self.state['latency'])
        self._save()
        return tuple(self._ticket(a) for a in self.state['actions']
                     if a['request'] == asdict(request) and a['committed'] and a['visible'] <= self.now())

    def wait_until(self, until):
        if until < self.now():
            raise ValueError('Clock cannot move backwards')
        self.state['calls'] += 1
        self._advance(until)
        self._save()

    def drain(self):
        self._advance(max([self.now()] + [a['due'] for a in self.state['actions']]))
        self._save()

    def snapshot(self):
        return Snapshot(tuple(self._ticket(a) for a in self.state['actions'] if a['committed']),
                        self.state['calls'])
