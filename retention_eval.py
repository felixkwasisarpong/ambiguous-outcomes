"""Frozen finite-retention experiment, fresh subprocesses and separate ledgers."""
import argparse
from collections import Counter
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import comparison
import comparison3
import evaluation
from durable import CRASH_EXIT, Fault, Journal
from hybrid import hybrid
from retention_policy import window_guard
from retention_service import RetentionContract, RetentionService
from smoke import SYNTHETIC_REQUEST

ROOT = Path(__file__).resolve().parent
FILES = ('retention-v1.json', 'RETENTION.md', 'retention_eval.py', 'retention_service.py',
         'retention_policy.py', 'test_retention.py', 'comparison-v3.lock.json')
POLICIES = ('legacy_hybrid', 'window_guard')


def specification():
    cases = []

    def add(group, ttl, lookup, latency, fault, downtime, crashes, deadline=20, budget=12, control=False):
        cases.append({'id': f'retention-v1-{len(cases)+1:03d}', 'group': group,
                      'contract': asdict(RetentionContract(lookup=lookup, key_ttl=ttl,
                                                          max_request_latency=latency)),
                      'fault': asdict(fault), 'latency': latency, 'downtime': downtime,
                      'crashes': crashes, 'deadline': deadline, 'max_calls': budget,
                      'must_complete': control})

    for ttl in (None, 3, 6):
        for lookup in (False, True):
            for latency in (0, 1):
                add('completion_control', ttl, lookup, latency, Fault(lose_reply=False),
                    0, [], control=True)
    for ttl in (None, 3, 6):
        for mode in ('committed_reply_lost', 'pending', 'dropped'):
            fault = Fault(3, 2) if mode == 'pending' else Fault(drop_request=mode == 'dropped')
            for lookup in (False, True):
                for latency in (0, 1):
                    for downtime in (0, 2, 3, 5, 6, 9):
                        add('permanent_key_stress' if ttl is None else 'finite_key_stress',
                            ttl, lookup, latency, fault, downtime, ['after_dispatch'])
    for deadline in (0, 1):
        for budget in (0, 1):
            add('resource_boundary', 3, False, 1, Fault(3, 2), 6, ['after_dispatch'], deadline, budget)
    acceptance = json.loads((ROOT / 'evaluation-v1.json').read_text())['acceptance']
    return {'version': 'retention-v1', 'construction': 'complete Cartesian grid, no sampling',
            'scope': 'same author, separately implemented synthetic service; no external validation',
            'acceptance': acceptance, 'cases': cases}


def write_new(path, value):
    with Path(path).open('x') as handle:
        handle.write(json.dumps(value, indent=2, sort_keys=True) + '\n')


def freeze():
    case_path, lock_path = ROOT / 'retention-v1.json', ROOT / 'retention-v1.lock.json'
    if case_path.exists() or lock_path.exists():
        raise FileExistsError('Retention v1 is immutable; use a new version')
    comparison3.verify()
    write_new(case_path, specification())
    lock = {'source_sha256': {name: evaluation.digest(ROOT / name) for name in FILES}}
    write_new(lock_path, lock)
    return lock


def verify():
    comparison3.verify()
    lock = json.loads((ROOT / 'retention-v1.lock.json').read_text())
    if set(lock['source_sha256']) != set(FILES):
        raise ValueError('Unexpected frozen file list')
    for name, expected in lock['source_sha256'].items():
        if evaluation.digest(ROOT / name) != expected:
            raise ValueError('Frozen retention source changed: ' + name)
    return lock


def worker(policy, service_path, journal_path, point, deadline, max_calls):
    def crash(actual):
        if point == actual:
            os._exit(CRASH_EXIT)
    service, journal = RetentionService(service_path, crash), Journal(journal_path, crash)
    try:
        controller = {'legacy_hybrid': hybrid, 'window_guard': window_guard}[policy]
        return asdict(controller(journal, service.tools(), SYNTHETIC_REQUEST,
                                 deadline=deadline, max_calls=max_calls, crash=crash))
    finally:
        journal.close()


def launch(policy, service, journal, point, deadline, max_calls):
    return subprocess.run([sys.executable, '-B', str(ROOT / 'retention_eval.py'), '--worker',
                           policy, str(service), str(journal), '--crash', point,
                           '--deadline', str(deadline), '--max-calls', str(max_calls)],
                          capture_output=True, text=True, timeout=15)


def run_case(case, policy):
    attempts, execution_error = [], None
    report = {'status': 'execution_error', 'ticket_id': None, 'observations': []}
    with tempfile.TemporaryDirectory() as directory:
        sp, jp = Path(directory) / 'service.json', Path(directory) / 'journal.db'
        RetentionService.initialize(sp, RetentionContract(**case['contract']),
                                    Fault(**case['fault']), case['latency'])
        try:
            for point in case['crashes'] + ['']:
                completed = launch(policy, sp, jp, point, case['deadline'], case['max_calls'])
                reached = completed.returncode == CRASH_EXIT
                attempts.append({'requested_crash': point or None, 'exit_code': completed.returncode,
                                 'crash_reached': reached})
                if reached and point:
                    RetentionService(sp).elapse(case['downtime'])
                    continue
                if completed.returncode:
                    raise RuntimeError('Worker failed')
                report = json.loads(completed.stdout)
                break
        except Exception as error:
            # Retain failed attempts without publishing local paths or tracebacks.
            execution_error = type(error).__name__
        service, journal = RetentionService(sp), Journal(jp)
        try:
            stopped = service.now()
            before = [asdict(t) for t in service.snapshot().effects]
            service.drain()
            snapshot = service.snapshot()
            ledger = [asdict(t) for t in snapshot.effects]
            row = journal.get('synthetic-operation')
            used = row['used'] if row else 0
            return {'id': case['id'], 'group': case['group'], 'must_complete': case['must_complete'],
                    'report': report, 'attempts': attempts, 'execution_error': execution_error,
                    'controller_stopped_at': stopped, 'reserved_calls': used,
                    'service_committed_call_counter': snapshot.calls,
                    'budget_violation': used > case['max_calls'],
                    'at_stop': evaluation.score(report, before, asdict(SYNTHETIC_REQUEST)),
                    'eventual': evaluation.score(report, ledger, asdict(SYNTHETIC_REQUEST)),
                    'committed_effects_ledger': ledger}
        finally:
            journal.close()


def summarize(cases, policies, acceptance):
    summaries = {name: evaluation.summarize(rows, acceptance) for name, rows in policies.items()}
    permanent = [i for i, c in enumerate(cases) if c['contract']['key_ttl'] is None]
    permanent_summaries = {name: evaluation.summarize([rows[i] for i in permanent], acceptance)
                           for name, rows in policies.items()}
    # Legacy ignores the new TTL contract and is intentionally outside its design
    # envelope on finite keys. Its failures remain visible but do not fail the
    # candidate gate. Execution errors and resource violations always fail it.
    gates = {'candidate_safety_and_controls': summaries['window_guard']['accepted'],
             'permanent_key_safety_and_controls': all(s['accepted'] for s in permanent_summaries.values()),
             'no_execution_errors': all(s['counts']['execution_errors'] == 0 for s in summaries.values()),
             'no_budget_violations': all(s['counts']['budget_violations'] == 0 for s in summaries.values())}
    return summaries, permanent_summaries, gates


def run():
    lock = verify()
    spec = json.loads((ROOT / 'retention-v1.json').read_text())
    policies = {}
    for policy in POLICIES:
        policies[policy] = []
        for index, case in enumerate(spec['cases'], 1):
            policies[policy].append(run_case(case, policy))
            if index % 48 == 0 or index == len(spec['cases']):
                print(f'Completed {policy}: {index}/{len(spec["cases"])}', file=sys.stderr, flush=True)
    summaries, permanent, gates = summarize(spec['cases'], policies, spec['acceptance'])
    unresolved = [r for r in policies['window_guard'] if not r['eventual']['safe_verified_completion']]
    return {'version': 'retention-v1', 'frozen_sha256': lock['source_sha256'],
            'lock_sha256': evaluation.digest(ROOT / 'retention-v1.lock.json'),
            'external_api_calls': 0, 'model_calls': 0,
            'fresh_cases_per_policy': len(spec['cases']), 'reused_rows': 0,
            'gates': gates, 'accepted': all(gates.values()), 'summaries': summaries,
            'permanent_key_summaries': permanent,
            'comparison': comparison.paired(spec['cases'], policies['legacy_hybrid'], policies['window_guard']),
            'candidate_noncompletion_statuses': dict(sorted(Counter(r['report']['status'] for r in unresolved).items())),
            'candidate_unverified_but_one_eventual_effect': sum(r['eventual']['committed_effects'] == 1 for r in unresolved),
            'policies': policies}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--freeze', action='store_true')
    mode.add_argument('--worker', nargs=3)
    mode.add_argument('--output')
    parser.add_argument('--crash', default='')
    parser.add_argument('--deadline', type=int, default=20)
    parser.add_argument('--max-calls', type=int, default=12)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(*args.worker, args.crash, args.deadline, args.max_calls), sort_keys=True))
    elif args.freeze:
        print(json.dumps(freeze(), sort_keys=True))
    else:
        if Path(args.output).exists():
            raise FileExistsError('Keep prior attempts; choose a new output path')
        output = run()
        write_new(args.output, output)
        if not output['accepted']:
            raise SystemExit(1)
