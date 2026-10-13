from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import unittest

from durable import CRASH_EXIT, Exhausted, Fault, Journal, Tools
from reconciliation_eval import TARGETS, check_results, launch, run_case, specification
from reconciled_policy import reconciled_guard
from readback_policy import readback_first
from recovery_journal import RecoveryJournal
from retention_service import RetentionContract, RetentionService
from smoke import SYNTHETIC_REQUEST as REQUEST, Ticket
from test_retention import case
import retention_eval

ROOT = Path(__file__).resolve().parent


class EvidenceJournalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'journal.db'
        self.journal = RecoveryJournal(self.path)
        self.addCleanup(self.journal.close)
        self.contract = RetentionContract()
        self.original = self.journal.prepare_strategy('op', REQUEST, self.contract, 20, 12, 'test')

    def test_reservation_records_latest_attempt_without_refreshing_epoch(self):
        self.journal.reserve_create('op', 0, new_epoch=True)
        self.journal.reserve_create('op', 2)
        evidence = self.journal.window('op')
        self.assertEqual((evidence['epoch_at'], evidence['last_dispatch_at']), (0, 2))
        self.assertEqual(self.journal.get('op')['used'], 2)
        self.assertEqual(self.journal.get('op')['submitted_at'], 0)

    def test_fresh_attempt_preserves_original_identity_and_limits(self):
        self.journal.reserve_create('op', 0, new_epoch=True)
        self.journal.reserve_create('op', 7, new_epoch=True)
        reopened = RecoveryJournal(self.path)
        try:
            saved = reopened.get('op')
            self.assertEqual(saved['op_key'], self.original['op_key'])
            self.assertEqual((saved['submitted_at'], saved['used'], saved['deadline'], saved['max_calls']), (0, 2, 20, 12))
            self.assertEqual((reopened.window('op')['epoch_at'], reopened.window('op')['last_dispatch_at']), (7, 7))
        finally:
            reopened.close()

    def test_atomic_failure_rolls_back_budget_and_dispatch_evidence(self):
        def fail(point):
            raise RuntimeError(point)
        self.journal.crash = fail
        with self.assertRaises(RuntimeError):
            self.journal.reserve_create('op', 0, new_epoch=True)
        self.assertEqual(self.journal.get('op')['used'], 0)
        self.assertIsNone(self.journal.get('op')['submitted_at'])
        self.assertIsNone(self.journal.window('op')['last_dispatch_at'])

    def test_deadline_rejection_does_not_start_new_epoch(self):
        self.journal.reserve_create('op', 0, new_epoch=True)
        previous = dict(self.journal.window('op'))
        with self.assertRaises(Exhausted):
            self.journal.reserve_create('op', 20, new_epoch=True)
        self.assertEqual(dict(self.journal.window('op')), previous)
        self.assertEqual(self.journal.get('op')['used'], 1)

    def test_strategy_switch_rejected(self):
        with self.assertRaises(ValueError):
            self.journal.prepare_strategy('op', REQUEST, self.contract, 20, 12, 'different')

    def test_missing_historical_dispatch_evidence_rejected(self):
        self.journal.prepare('old', REQUEST, self.contract, 20, 12)
        self.journal.reserve('old', 0, dispatch=True)
        with self.assertRaises(ValueError):
            self.journal.prepare_strategy('old', REQUEST, self.contract, 20, 12, 'test')

    def test_changed_request_and_inflated_budget_rejected(self):
        for request, budget in ((replace(REQUEST, subject='Other fictional action'), 12), (REQUEST, 24)):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                self.journal.prepare_strategy('op', request, self.contract, 20, budget, 'test')

    def test_clock_reversal_cannot_rewrite_evidence(self):
        self.journal.reserve_create('op', 5, new_epoch=True)
        with self.assertRaises(ValueError):
            self.journal.reserve_create('op', 4)
        self.assertEqual(self.journal.window('op')['last_dispatch_at'], 5)


class ReconciliationPolicyTests(unittest.TestCase):
    def test_all_fifteen_disclosed_development_targets(self):
        cases = {c['id']: c for c in json.loads((ROOT / 'retention-v1.json').read_text())['cases']}
        for name in TARGETS:
            with self.subTest(case=name):
                row = run_case(cases[name], 'reconciled_guard')
                self.assertIsNone(row['execution_error'])
                self.assertTrue(row['eventual']['safe_verified_completion'])
                self.assertEqual(row['eventual']['excess_effects'], 0)

    def test_negative_readback_with_unknown_bound_never_renews_permission(self):
        spec = case(lookup=True, fault=Fault(drop_request=True), downtime=6)
        spec['contract']['settlement_bound'] = None
        for policy in ('reconciled_guard', 'readback_first'):
            with self.subTest(policy=policy):
                row = run_case(spec, policy)
                self.assertEqual(row['eventual']['committed_effects'], 0)
                self.assertEqual(row['report']['status'], 'unresolved_no_settlement_bound')
                self.assertFalse(any(o['kind'] == 'settled_absence' for o in row['report']['observations']))

    def test_expired_window_without_lookup_still_abstains(self):
        for fault in (Fault(), Fault(drop_request=True)):
            row = run_case(case(fault=fault), 'reconciled_guard')
            self.assertEqual(row['report']['status'], 'retry_window_expired')
            self.assertEqual(row['eventual']['excess_effects'], 0)
            self.assertFalse(row['eventual']['safe_verified_completion'])

    def test_exit_after_absence_requires_fresh_readback(self):
        spec = case(ttl=2, lookup=True, downtime=4, fault=Fault(drop_request=True),
                    crashes=['after_dispatch', 'after_absence'])
        row = run_case(spec, 'reconciled_guard')
        self.assertEqual(sum(a['crash_reached'] for a in row['attempts']), 2)
        self.assertTrue(row['eventual']['safe_verified_completion'])
        self.assertIn('settled_absence', [o['kind'] for o in row['report']['observations']])

    def test_actual_exit_inside_reservation_rolls_back(self):
        row = run_case(case(ttl=6, lookup=True, downtime=2, crashes=['during_attempt_reservation']), 'reconciled_guard')
        self.assertTrue(row['attempts'][0]['crash_reached'])
        self.assertTrue(row['eventual']['safe_verified_completion'])
        self.assertEqual(row['reserved_calls'], 2)

    def test_confirmation_exit_keeps_success_after_deadline(self):
        row = run_case(case(lookup=True, downtime=30, crashes=['after_confirmation']), 'reconciled_guard')
        self.assertTrue(row['eventual']['safe_verified_completion'])
        self.assertEqual(row['report']['observations'][0]['kind'], 'journal')

    def test_tight_budget_cannot_be_replenished_by_absence(self):
        spec = case(lookup=True, fault=Fault(drop_request=True), downtime=6)
        spec['max_calls'] = 2
        row = run_case(spec, 'reconciled_guard')
        self.assertEqual(row['reserved_calls'], 2)
        self.assertEqual(row['eventual']['committed_effects'], 0)
        self.assertEqual(row['report']['status'], 'budget_or_deadline_exhausted')

    def test_pending_replay_waits_through_latest_dispatch_not_original(self):
        # First attempt at t=0 was dropped. At t=2 a safe retry may be accepted
        # and not become visible until t=7. Reading absence at t=5 is insufficient.
        with tempfile.TemporaryDirectory() as folder:
            journal = RecoveryJournal(Path(folder) / 'journal.db')
            contract = RetentionContract(key_ttl=3, settlement_bound=5)
            state = {'now': 2, 'creates': 0, 'due': None, 'reads': []}
            def create(request, key):
                state['creates'] += 1
                if state['due'] is None:
                    state['due'] = state['now'] + 5
                raise TimeoutError('No response received')
            def find(request):
                state['reads'].append(state['now'])
                return (Ticket('ticket-1', REQUEST),) if state['now'] >= state['due'] else ()
            def wait(until):
                state['now'] = until
            try:
                journal.prepare_strategy('synthetic-operation', REQUEST, contract, 20, 12, 'reconciled_guard')
                journal.reserve_create('synthetic-operation', 0, new_epoch=True)
                report = reconciled_guard(journal, Tools(contract, lambda: state['now'], create, find, wait), REQUEST)
                self.assertEqual(report.status, 'success_observed')
                self.assertEqual(state['creates'], 1)
                self.assertEqual(state['reads'], [7])
                self.assertEqual(journal.window('synthetic-operation')['last_dispatch_at'], 2)
            finally:
                journal.close()

    def test_negative_proof_cannot_be_reused_after_renewed_attempt_times_out(self):
        # Expired dropped attempt at t=0, absence at t=6, then a new ambiguous
        # attempt. The next negative proof must cover the renewed t=6 attempt.
        with tempfile.TemporaryDirectory() as folder:
            journal = RecoveryJournal(Path(folder) / 'journal.db')
            contract = RetentionContract(key_ttl=1, settlement_bound=5, max_request_latency=1)
            state = {'now': 6, 'creates': 0, 'due': None, 'reads': []}
            def create(request, key):
                state['creates'] += 1
                state['now'] += 1
                state['due'] = state['now'] + 5
                raise TimeoutError('No response received')
            def find(request):
                state['reads'].append(state['now'])
                return (Ticket('ticket-1', REQUEST),) if state['due'] is not None and state['now'] >= state['due'] else ()
            def wait(until):
                state['now'] = until
            try:
                journal.prepare_strategy('synthetic-operation', REQUEST, contract, 20, 12, 'reconciled_guard')
                journal.reserve_create('synthetic-operation', 0, new_epoch=True)
                report = reconciled_guard(journal, Tools(contract, lambda: state['now'], create, find, wait), REQUEST)
                self.assertEqual(report.status, 'success_observed')
                self.assertEqual(state['creates'], 1)
                self.assertEqual(state['reads'], [6, 12])
                self.assertEqual(journal.window('synthetic-operation')['epoch_at'], 6)
                self.assertEqual(journal.get('synthetic-operation')['submitted_at'], 0)
            finally:
                journal.close()

    def test_indistinguishable_expired_histories_require_different_retry_outcomes(self):
        histories, forced_effects = [], []
        for fault in (Fault(), Fault(drop_request=True)):
            with tempfile.TemporaryDirectory() as folder:
                sp, jp = Path(folder) / 'service.json', Path(folder) / 'journal.db'
                contract = RetentionContract(lookup=False, key_ttl=3)
                RetentionService.initialize(sp, contract, fault)
                completed = launch('reconciled_guard', sp, jp, 'after_dispatch', 20, 12)
                self.assertEqual(completed.returncode, CRASH_EXIT)
                service = RetentionService(sp)
                service.elapse(3)
                journal = RecoveryJournal(jp)
                try:
                    report = reconciled_guard(journal, service.tools(), REQUEST)
                    histories.append({'contract': asdict(contract), 'now': service.now(),
                                      'operation': dict(journal.get('synthetic-operation')),
                                      'evidence': dict(journal.window('synthetic-operation')),
                                      'report': asdict(report)})
                    # Evaluator-only forced unsafe replay exposes the hidden
                    # difference; this is never part of the policy's input.
                    service.create(REQUEST, journal.get('synthetic-operation')['op_key'])
                    forced_effects.append(len(service.snapshot().effects))
                finally:
                    journal.close()
        self.assertEqual(histories[0], histories[1])
        self.assertEqual(forced_effects, [2, 1])

    def test_no_lookup_does_not_repeat_calls_at_final_safe_send_time(self):
        row = run_case(case(ttl=3, downtime=0, fault=Fault(3, 2)), 'reconciled_guard')
        self.assertEqual(row['report']['status'], 'unresolved_before_expiry')
        self.assertLess(row['reserved_calls'], 6)
        self.assertFalse(row['eventual']['safe_verified_completion'])
        self.assertEqual(row['eventual']['committed_effects'], 1)

    def test_readback_first_verifies_positive_before_spending_a_write(self):
        row = run_case(case(lookup=True, downtime=6), 'readback_first')
        self.assertTrue(row['eventual']['safe_verified_completion'])
        self.assertEqual(row['reserved_calls'], 2)
        self.assertEqual(row['report']['observations'][0]['kind'], 'lookup')


class ReconciliationEvaluationTests(unittest.TestCase):
    def test_case_manifest_preserves_originals_and_adds_seventy_two(self):
        spec = specification()
        old = json.loads((ROOT / 'retention-v1.json').read_text())['cases']
        self.assertEqual(spec, specification())
        self.assertEqual(spec['cases'][:232], old)
        self.assertEqual(len(spec['cases']), 304)
        self.assertEqual(len({c['id'] for c in spec['cases']}), 304)
        self.assertEqual(len(TARGETS), 15)

    def test_candidate_loss_and_false_success_fail_gates(self):
        spec = specification()
        previous = json.loads((ROOT / 'retention-v1-results.json').read_text())['policies']['window_guard']
        cases = spec['cases'][:232]
        small = dict(spec, cases=cases)
        rows = deepcopy(previous)
        for row in rows:
            if row['id'] in TARGETS:
                row['eventual']['safe_verified_completion'] = True
        policies = {'window_guard': previous, 'readback_first': deepcopy(previous), 'reconciled_guard': rows}
        old_success = next(row for row in rows if row['eventual']['safe_verified_completion'] and row['id'] not in TARGETS)
        old_success['eventual']['safe_verified_completion'] = False
        self.assertFalse(check_results(small, policies, previous)[2]['no_old_guard_completion_losses'])
        old_success['eventual']['false_success'] = True
        self.assertFalse(check_results(small, policies, previous)[2]['all_policies_safe_and_controls'])

    def test_previous_sources_remain_frozen(self):
        retention_eval.verify()


if __name__ == '__main__':
    unittest.main()
