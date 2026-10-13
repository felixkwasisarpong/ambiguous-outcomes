from dataclasses import replace
import unittest

from delayed import Contract, DelayedService, FaultPlan, recover, run_fixtures
from smoke import SYNTHETIC_REQUEST as REQUEST


class DelayedTests(unittest.TestCase):
    def setUp(self):
        self.rows = {row["fixture"]: row for row in run_fixtures()["fixtures"]}

    def test_early_negative_read_hides_later_duplicate(self):
        row = self.rows["early_negative_read_retry"]
        self.assertEqual(row["at_stop"]["committed_effects"], 1)
        self.assertEqual(row["eventual"]["committed_effects"], 2)
        self.assertTrue(row["eventual"]["false_success"])
        self.assertEqual([e["committed_at"] for e in row["committed_effects_ledger"]], [0, 3])

    def test_waiting_bound_observes_delayed_commit_without_retry(self):
        row = self.rows["wait_for_delayed_commit"]
        self.assertEqual(row["controller_stopped_at"], 5)
        self.assertEqual(row["eventual"]["committed_effects"], 1)
        self.assertTrue(row["eventual"]["safe_verified_completion"])
        self.assertEqual(row["eventual"]["tool_calls"], 4)

    def test_stale_negative_read_does_not_trigger_create(self):
        row = self.rows["wait_for_stale_read"]
        self.assertIn("missing", row["report"]["observations"][1]["value"])
        self.assertEqual(row["committed_effects_ledger"][0]["committed_at"], 0)
        self.assertEqual(row["committed_effects_ledger"][0]["visible_at"], 4)
        self.assertEqual(row["eventual"]["committed_effects"], 1)
        self.assertTrue(row["eventual"]["safe_verified_completion"])

    def test_effect_after_deadline_remains_in_score(self):
        row = self.rows["deadline_before_commit"]
        self.assertEqual(row["controller_stopped_at"], 2)
        self.assertEqual(row["report"]["status"], "unresolved")
        self.assertEqual(row["at_stop"]["committed_effects"], 0)
        self.assertEqual(row["eventual"]["committed_effects"], 1)
        self.assertFalse(row["eventual"]["safe_verified_completion"])

    def test_absent_effect_retried_only_after_settlement_bound(self):
        row = self.rows["retry_after_settled_absence"]
        self.assertEqual(row["committed_effects_ledger"][0]["committed_at"], 5)
        self.assertTrue(row["eventual"]["safe_verified_completion"])

    def test_unknown_bound_cannot_justify_retry(self):
        row = self.rows["unknown_bound_abstains"]
        self.assertEqual(row["controller_stopped_at"], 0)
        self.assertEqual(row["report"]["status"], "unresolved")
        self.assertEqual(row["eventual"]["committed_effects"], 1)

    def test_invalid_contract_fixture_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "exceeds"):
            DelayedService(Contract(3), FaultPlan(3, 1))

    def test_budget_exhaustion_cannot_erase_pending_effect(self):
        service = DelayedService(Contract(5), FaultPlan(3, 2), max_calls=1)
        report = recover(service.tools(), REQUEST)
        self.assertEqual(report.status, "budget_exhausted")
        service.drain()
        self.assertEqual(len(service.snapshot().effects), 1)
        self.assertEqual(service.snapshot().calls, 1)

    def test_authorization_failure_has_no_effect(self):
        service = DelayedService(Contract(5), FaultPlan(3, 2))
        report = recover(service.tools(), replace(REQUEST, tenant="unapproved-tenant"))
        service.drain()
        self.assertEqual(report.status, "rejected")
        self.assertEqual(len(service.snapshot().effects), 0)

    def test_visible_record_with_different_intent_is_not_a_match(self):
        service = DelayedService(Contract(0), FaultPlan())
        with self.assertRaises(TimeoutError):
            service.tools().create(REQUEST)
        self.assertEqual(service.tools().find(replace(REQUEST, intent="another-intent")), ())

    def test_immediate_visibility_at_zero_bound(self):
        service = DelayedService(Contract(0), FaultPlan())
        report = recover(service.tools(), REQUEST)
        self.assertEqual(report.status, "success_observed")
        self.assertEqual(service.snapshot().calls, 2)

    def test_deadline_at_bound_does_not_start_a_new_request(self):
        service = DelayedService(Contract(5), FaultPlan(drop_request=True))
        report = recover(service.tools(), REQUEST, deadline=5)
        self.assertEqual(report.status, "unresolved")
        self.assertEqual(service.snapshot().effects, ())

    def test_deterministic_repeated_manifest(self):
        self.assertEqual(run_fixtures(), run_fixtures())


if __name__ == "__main__":
    unittest.main()
