"""Frozen follow-up: bounded negative readback and two TTL-aware policies."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import comparison
import evaluation
import retention_eval
from durable import CRASH_EXIT, Fault, Journal
from readback_policy import readback_first
from reconciled_policy import reconciled_guard
from recovery_journal import RecoveryJournal
from retention_policy import window_guard
from retention_service import RetentionContract, RetentionService
from smoke import SYNTHETIC_REQUEST

ROOT = Path(__file__).resolve().parent
POLICIES = ('window_guard', 'readback_first', 'reconciled_guard')
FILES = ('reconciliation-v1.json', 'RECONCILIATION.md', 'reconciliation_eval.py',
         'reconciled_policy.py', 'readback_policy.py', 'recovery_journal.py',
         'test_reconciliation.py', 'retention-v1.lock.json', 'retention-v1-results.json')
TARGETS = ['retention-v1-147', 'retention-v1-148', 'retention-v1-149', 'retention-v1-150',
           'retention-v1-152', 'retention-v1-153', 'retention-v1-154', 'retention-v1-155',
           'retention-v1-156', 'retention-v1-187', 'retention-v1-221', 'retention-v1-222',
           'retention-v1-226', 'retention-v1-227', 'retention-v1-228']


def specification():
    previous = json.loads((ROOT / 'retention-v1.json').read_text())
    cases = previous['cases']
    for ttl in (2, 5):
        for bounded in (False, True):
            for latency in (0, 1):
                for mode in ('reply_lost', 'pending', 'dropped'):
                    for crashes in (['before_dispatch'] * 2, ['after_dispatch', 'after_absence'],
                                    ['during_confirmation'] * 2):
                        fault = Fault(2, 2) if mode == 'pending' else Fault(drop_request=mode == 'dropped')
                        cases.append({'id': f'reconciliation-v1-{len(cases)-231:03d}',
                            'group': 'new_reconciliation_stress', 'must_complete': False,
                            'contract': asdict(RetentionContract(lookup=True, key_ttl=ttl,
                                settlement_bound=5 if bounded else None, max_request_latency=latency+1)),
                            'fault': asdict(fault), 'latency': latency, 'downtime': 2,
                            'deadline': 20, 'max_calls': 12, 'crashes': list(crashes)})
    return {'version': 'reconciliation-v1', 'acceptance': previous['acceptance'],
            'targets': TARGETS, 'old_case_count': 232,
            'scope': 'same-author synthetic regression and newly specified stress cases', 'cases': cases}


def freeze():
    case_path, lock_path = ROOT / 'reconciliation-v1.json', ROOT / 'reconciliation-v1.lock.json'
    if case_path.exists() or lock_path.exists():
        raise FileExistsError('Already frozen; use a new version')
    retention_eval.verify()
    retention_eval.write_new(case_path, specification())
    lock = {'source_sha256': {name: evaluation.digest(ROOT / name) for name in FILES}}
    retention_eval.write_new(lock_path, lock)
    return lock


def verify():
    retention_eval.verify()
    lock = json.loads((ROOT / 'reconciliation-v1.lock.json').read_text())
    if set(lock['source_sha256']) != set(FILES):
        raise ValueError('Unexpected frozen sources')
    for name, expected in lock['source_sha256'].items():
        if evaluation.digest(ROOT / name) != expected:
            raise ValueError('Frozen reconciliation source changed: ' + name)
    return lock


def worker(policy, service_path, journal_path, point, deadline, max_calls):
    def crash(actual):
        if actual == point:
            os._exit(CRASH_EXIT)
    service = RetentionService(service_path, crash)
    journal = (Journal if policy == 'window_guard' else RecoveryJournal)(journal_path, crash)
    try:
        controller = {'window_guard': window_guard, 'readback_first': readback_first,
                      'reconciled_guard': reconciled_guard}[policy]
        return asdict(controller(journal, service.tools(), SYNTHETIC_REQUEST,
                                 deadline=deadline, max_calls=max_calls, crash=crash))
    finally:
        journal.close()


def launch(policy, service, journal, point, deadline, max_calls):
    return subprocess.run([sys.executable, '-B', str(ROOT / 'reconciliation_eval.py'), '--worker',
        policy, str(service), str(journal), '--crash', point, '--deadline', str(deadline),
        '--max-calls', str(max_calls)], capture_output=True, text=True, timeout=15)


def run_case(case, policy):
    # Original service, runner and scorer; only the worker executable changes.
    with patch.object(retention_eval, 'launch', launch):
        return retention_eval.run_case(case, policy)


def check_results(spec, policies, original):
    cases = spec['cases']
    summaries = {name: evaluation.summarize(rows, spec['acceptance']) for name, rows in policies.items()}
    pairs = {name: comparison.paired(cases, policies[name], policies['reconciled_guard'])
             for name in ('window_guard', 'readback_first')}
    indexed = {r['id']: r for r in policies['reconciled_guard']}
    fixed = {name: indexed[name]['eventual']['safe_verified_completion'] for name in TARGETS}
    old_ids = {c['id'] for c in cases[:spec['old_case_count']]}
    old_losses = [p['id'] for p in pairs['window_guard']['pairs']
                  if p['id'] in old_ids and p['outcome'] == 'loss']
    gates = {'all_policies_safe_and_controls': all(s['accepted'] for s in summaries.values()),
             'original_guard_reproduced': policies['window_guard'][:spec['old_case_count']] == original,
             'all_15_declared_targets_fixed': all(fixed.values()),
             'no_old_guard_completion_losses': not old_losses}
    return summaries, pairs, gates, fixed, old_losses


def run():
    lock = verify()
    spec = json.loads((ROOT / 'reconciliation-v1.json').read_text())
    original = json.loads((ROOT / 'retention-v1-results.json').read_text())['policies']['window_guard']
    policies = {}
    for policy in POLICIES:
        policies[policy] = []
        for index, case in enumerate(spec['cases'], 1):
            policies[policy].append(run_case(case, policy))
            if index % 64 == 0 or index == len(spec['cases']):
                print(f'Completed {policy}: {index}/{len(spec["cases"])}', file=sys.stderr, flush=True)
    summaries, pairs, gates, fixed, losses = check_results(spec, policies, original)
    return {'version': 'reconciliation-v1', 'frozen_sha256': lock['source_sha256'],
            'lock_sha256': evaluation.digest(ROOT / 'reconciliation-v1.lock.json'),
            'external_api_calls': 0, 'model_calls': 0, 'fresh_cases_per_policy': len(spec['cases']),
            'reused_rows': 0, 'accepted': all(gates.values()), 'gates': gates,
            'target_fixes': fixed, 'old_guard_completion_losses': losses,
            'summaries': summaries, 'comparisons': pairs, 'policies': policies}


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
            raise FileExistsError('Keep previous attempts; choose a new path')
        report = run()
        retention_eval.write_new(args.output, report)
        if not report['accepted']:
            raise SystemExit(1)
