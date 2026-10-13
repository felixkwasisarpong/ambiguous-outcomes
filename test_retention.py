from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import comparison3
import evaluation
from durable import Fault, Journal
from retention_eval import run_case, specification, summarize, write_new
from retention_policy import window_guard
from retention_service import RetentionContract, RetentionService
from smoke import SYNTHETIC_REQUEST as REQUEST


def case(ttl=3, lookup=False, downtime=3, latency=0, fault=None, crashes=None):
    return {'id': 'development-case', 'group': 'development', 'must_complete': False,
            'contract': asdict(RetentionContract(lookup=lookup, key_ttl=ttl,
                                                max_request_latency=latency)),
            'fault': asdict(fault or Fault()), 'latency': latency, 'downtime': downtime,
            'crashes': ['after_dispatch'] if crashes is None else crashes,
            'deadline': 20, 'max_calls': 12}


class ServiceRetentionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'service.json'
        self.addCleanup(self.directory.cleanup)

    def service(self, contract=RetentionContract(key_ttl=3), fault=Fault(), latency=0):
        RetentionService.initialize(self.path, contract, fault, latency)
        return RetentionService(self.path)

    def test_replay_does_not_renew_window_and_exact_expiry_allows_new_effect(self):
        service = self.service()
        with self.assertRaises(TimeoutError):
            service.create(REQUEST, 'key')
        service.elapse(2)
        self.assertEqual(service.create(REQUEST, 'key').id, 'ticket-1')
        service.elapse(1)
        self.assertEqual(service.create(REQUEST, 'key').id, 'ticket-2')
        self.assertEqual(len(service.snapshot().effects), 2)

    def test_expiration_keeps_pending_effect(self):
        service = self.service(RetentionContract(key_ttl=2), Fault(3, 2))
        with self.assertRaises(TimeoutError):
            service.create(REQUEST, 'key')
        service.elapse(2)
        service.create(REQUEST, 'key')
        self.assertEqual(len(service.snapshot().effects), 1)
        service.drain()
        self.assertEqual(len(service.snapshot().effects), 2)

    def test_permanent_key_survives_long_downtime(self):
        service = self.service(RetentionContract(key_ttl=None))
        with self.assertRaises(TimeoutError):
            service.create(REQUEST, 'key')
        service.elapse(100)
        service.create(REQUEST, 'key')
        self.assertEqual(len(service.snapshot().effects), 1)

    def test_dropped_request_does_not_reserve_key_or_effect(self):
        service = self.service(fault=Fault(drop_request=True))
        with self.assertRaises(TimeoutError):
            service.create(REQUEST, 'key')
        service.elapse(9)
        service.create(REQUEST, 'key')
        self.assertEqual(len(service.snapshot().effects), 1)

    def test_readback_remains_stale_until_visibility_bound(self):
        service = self.service(fault=Fault(3, 2))
        with self.assertRaises(TimeoutError):
            service.create(REQUEST, 'key')
        service.elapse(3)
        self.assertEqual(service.find(REQUEST), ())
        service.elapse(2)
        self.assertEqual(len(service.find(REQUEST)), 1)

    def test_clock_progress_and_state_survive_reopen(self):
        service = self.service(RetentionContract(key_ttl=6, max_request_latency=1), latency=1)
        with self.assertRaises(TimeoutError):
            service.create(REQUEST, 'key')
        self.assertEqual(service.now(), 1)
        service.elapse(4)
        restored = RetentionService(self.path)
        self.assertEqual(restored.now(), 5)
        self.assertEqual(len(restored.find(REQUEST)), 1)
        self.assertEqual(restored.now(), 6)

    def test_tenant_and_live_key_payload_mismatch_rejected(self):
        service = self.service(fault=Fault(lose_reply=False))
        service.create(REQUEST, 'key')
        with self.assertRaises(ValueError):
            service.create(replace(REQUEST, subject='Different fictional task'), 'key')
        with self.assertRaises(PermissionError):
            service.create(replace(REQUEST, tenant='other-tenant'), 'key')
        self.assertEqual(len(service.snapshot().effects), 1)

    def test_existing_state_cannot_be_reset(self):
        self.service()
        original = self.path.read_bytes()
        with self.assertRaises(FileExistsError):
            self.service()
        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_or_untruthful_contract_rejected(self):
        for contract, fault, latency in [
            (RetentionContract(key_ttl=0), Fault(), 0),
            (RetentionContract(max_request_latency=0), Fault(), 1),
            (RetentionContract(settlement_bound=2), Fault(3, 2), 0),
        ]:
            with self.subTest(contract=contract), self.assertRaises(ValueError):
                RetentionService.initialize(self.path, contract, fault, latency)


class WindowGuardTests(unittest.TestCase):
    def test_exact_expiry_blocks_duplicate_from_legacy_replay(self):
        candidate = run_case(case(), 'window_guard')
        legacy = run_case(case(), 'legacy_hybrid')
        self.assertEqual(candidate['report']['status'], 'retry_window_expired')
        self.assertEqual(candidate['eventual']['excess_effects'], 0)
        self.assertEqual(legacy['eventual']['excess_effects'], 1)
        self.assertTrue(legacy['eventual']['false_success'])

    def test_safe_replay_before_expiry_completes(self):
        row = run_case(case(downtime=2), 'window_guard')
        self.assertTrue(row['eventual']['safe_verified_completion'])
        self.assertEqual(row['reserved_calls'], 2)

    def test_expired_key_can_confirm_via_positive_readback(self):
        row = run_case(case(lookup=True), 'window_guard')
        self.assertTrue(row['eventual']['safe_verified_completion'])
        self.assertEqual(row['eventual']['excess_effects'], 0)
        self.assertEqual(row['report']['observations'][-1]['kind'], 'lookup')

    def test_stale_negative_readback_does_not_enable_an_expired_write(self):
        row = run_case(case(ttl=2, lookup=True, downtime=2, fault=Fault(3, 2)), 'window_guard')
        self.assertTrue(row['eventual']['safe_verified_completion'])
        self.assertEqual(row['eventual']['committed_effects'], 1)
        self.assertGreaterEqual(row['controller_stopped_at'], 5)

    def test_dropped_request_after_expiry_remains_explicitly_unresolved(self):
        row = run_case(case(lookup=True, fault=Fault(drop_request=True)), 'window_guard')
        self.assertEqual(row['report']['status'], 'retry_window_expired')
        self.assertEqual(row['eventual']['committed_effects'], 0)
        self.assertFalse(row['eventual']['safe_verified_completion'])

    def test_request_transit_can_cross_actual_expiry(self):
        spec = case(downtime=2, latency=1)
        guarded = run_case(spec, 'window_guard')
        legacy = run_case(spec, 'legacy_hybrid')
        self.assertEqual(guarded['report']['status'], 'retry_window_expired')
        self.assertEqual(guarded['eventual']['excess_effects'], 0)
        self.assertEqual(legacy['eventual']['excess_effects'], 1)

    def test_first_request_is_allowed_even_when_ttl_is_shorter_than_transit(self):
        row = run_case(case(ttl=1, latency=2, fault=Fault(lose_reply=False), crashes=[]), 'window_guard')
        self.assertTrue(row['eventual']['safe_verified_completion'])

    def test_call_deadline_is_exclusive(self):
        spec = case(crashes=[])
        spec['deadline'] = 0
        row = run_case(spec, 'window_guard')
        self.assertEqual(row['reserved_calls'], 0)
        self.assertEqual(row['eventual']['committed_effects'], 0)

    def test_origin_key_and_budget_survive_repeated_pre_dispatch_exits(self):
        spec = case(ttl=6, downtime=1, crashes=['before_dispatch', 'before_dispatch'])
        spec['max_calls'] = 4
        row = run_case(spec, 'window_guard')
        self.assertEqual(sum(a['crash_reached'] for a in row['attempts']), 2)
        self.assertLessEqual(row['reserved_calls'], 4)
        self.assertEqual(row['eventual']['committed_effects'], 1)
        with tempfile.TemporaryDirectory() as directory:
            sp, jp = Path(directory) / 'service.json', Path(directory) / 'journal.db'
            RetentionService.initialize(sp, RetentionContract(key_ttl=6), Fault(3, 2))
            service, journal = RetentionService(sp), Journal(jp)
            try:
                saved = journal.prepare('synthetic-operation', REQUEST, service.contract, 20, 12)
                report = window_guard(journal, service.tools(), REQUEST)
                current = journal.get('synthetic-operation')
                self.assertEqual(current['op_key'], saved['op_key'])
                self.assertEqual(current['submitted_at'], 0)
                self.assertEqual(report.status, 'success_observed')
            finally:
                journal.close()

    def test_service_transaction_rollback_and_commit_crashes(self):
        for point in ('during_service_commit', 'after_service_commit'):
            with self.subTest(point=point):
                spec = case(ttl=6, lookup=True, downtime=0, crashes=[point])
                row = run_case(spec, 'window_guard')
                self.assertTrue(row['attempts'][0]['crash_reached'])
                self.assertTrue(row['eventual']['safe_verified_completion'])
                self.assertEqual(row['eventual']['committed_effects'], 1)

    def test_confirmed_journal_survives_confirmation_exit_and_deadline(self):
        row = run_case(case(lookup=True, downtime=30, crashes=['after_confirmation']), 'window_guard')
        self.assertTrue(row['eventual']['safe_verified_completion'])
        self.assertEqual(row['report']['observations'][0]['kind'], 'journal')

    def test_configuration_changes_rejected_without_creating_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            sp, jp = Path(directory) / 'service.json', Path(directory) / 'journal.db'
            RetentionService.initialize(sp)
            service, journal = RetentionService(sp), Journal(jp)
            try:
                journal.prepare('synthetic-operation', REQUEST, service.contract, 20, 12)
                result = window_guard(journal, service.tools(), REQUEST, max_calls=24)
                self.assertEqual(result.status, 'rejected')
                self.assertEqual(service.snapshot().effects, ())
            finally:
                journal.close()


class RetentionEvaluationTests(unittest.TestCase):
    def test_grid_is_complete_and_deterministic(self):
        spec = specification()
        self.assertEqual(spec, specification())
        self.assertEqual(len(spec['cases']), 232)
        self.assertEqual(len({c['id'] for c in spec['cases']}), 232)
        self.assertEqual(sum(c['must_complete'] for c in spec['cases']), 12)
        self.assertEqual(sum(c['group'] == 'finite_key_stress' for c in spec['cases']), 144)

    def test_acceptance_does_not_hide_legacy_failure_or_accept_candidate_failure(self):
        spec = case()
        policies = {p: [run_case(spec, p)] for p in ('legacy_hybrid', 'window_guard')}
        acceptance = specification()['acceptance']
        summaries, _, gates = summarize([spec], policies, acceptance)
        self.assertFalse(summaries['legacy_hybrid']['accepted'])
        self.assertTrue(all(gates.values()))
        broken = deepcopy(policies)
        broken['window_guard'][0]['eventual']['false_success'] = True
        self.assertFalse(summarize([spec], broken, acceptance)[2]['candidate_safety_and_controls'])

    def test_eventual_scoring_catches_duplicate_that_arrives_after_stop(self):
        report = {'status': 'success_observed', 'ticket_id': 'ticket-2'}
        ledger = [{'id': 'ticket-2', 'request': asdict(REQUEST)}]
        self.assertTrue(evaluation.score(report, ledger, asdict(REQUEST))['safe_verified_completion'])
        ledger.append({'id': 'ticket-1', 'request': asdict(REQUEST)})
        scored = evaluation.score(report, ledger, asdict(REQUEST))
        self.assertTrue(scored['false_success'])
        self.assertEqual(scored['excess_effects'], 1)

    def test_timeout_is_recorded_without_private_paths(self):
        with patch('retention_eval.launch', side_effect=subprocess.TimeoutExpired('/private/synthetic', 15)):
            row = run_case(case(), 'window_guard')
        self.assertEqual(row['execution_error'], 'TimeoutExpired')
        self.assertNotIn('/private/', json.dumps(row))

    def test_existing_attempt_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'attempt.json'
            write_new(path, {'accepted': False})
            with self.assertRaises(FileExistsError):
                write_new(path, {'accepted': True})
            self.assertFalse(json.loads(path.read_text())['accepted'])

    def test_previous_frozen_versions_unchanged(self):
        comparison3.verify()


if __name__ == '__main__':
    unittest.main()
