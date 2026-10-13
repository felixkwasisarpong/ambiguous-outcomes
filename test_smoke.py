"""Ground-truth assertions for the phase-zero simulator, using only unittest."""

from dataclasses import replace
import unittest

from smoke import (
    Contract, Fault, Observation, Report, SYNTHETIC_REQUEST as REQUEST,
    Ticket, TicketService, Snapshot, evaluate, run_fixtures, same_key_retry,
)


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.rows = {row["fixture"]: row for row in run_fixtures()["fixtures"]}

    def test_blind_retry_duplicates_committed_action(self):
        row = self.rows["blind_retry_after_commit"]
        self.assertEqual(row["oracle"]["committed_effects"], 2)
        self.assertEqual(row["oracle"]["excess_effects"], 1)
        self.assertTrue(row["oracle"]["false_success"])
        self.assertFalse(row["oracle"]["safe_verified_completion"])

    def test_same_key_retry_preserves_single_effect(self):
        row = self.rows["same_key_retry_after_commit"]
        self.assertEqual(row["oracle"]["committed_effects"], 1)
        self.assertEqual(row["oracle"]["tool_calls"], 2)
        self.assertTrue(row["oracle"]["safe_verified_completion"])
        self.assertFalse(row["oracle"]["false_success"])

    def test_opaque_precommit_timeout_has_no_effect(self):
        row = self.rows["opaque_before_commit"]
        self.assertEqual(row["oracle"]["committed_effects"], 0)
        self.assertEqual(row["oracle"]["tool_calls"], 1)
        self.assertEqual(row["report"]["status"], "unresolved")
        self.assertFalse(row["oracle"]["safe_verified_completion"])

    def test_opaque_postcommit_timeout_is_observationally_identical(self):
        row = self.rows["opaque_after_commit"]
        self.assertEqual(row["oracle"]["committed_effects"], 1)
        self.assertEqual(row["oracle"]["tool_calls"], 1)
        self.assertEqual(row["report"], self.rows["opaque_before_commit"]["report"])
        self.assertFalse(row["oracle"]["safe_verified_completion"])
        self.assertFalse(row["oracle"]["false_success"])


class OracleAndContractTests(unittest.TestCase):
    def test_clean_create_control(self):
        service = TicketService(Contract.IDEMPOTENT, Fault.NONE)
        report = same_key_retry(service.tools(), REQUEST)
        self.assertTrue(evaluate(REQUEST, report, service.snapshot())["safe_verified_completion"])
        self.assertEqual(service.snapshot().calls, 1)

    def test_key_payload_mismatch_cannot_create_another_effect(self):
        service = TicketService(Contract.IDEMPOTENT, Fault.NONE)
        service.tools().create(REQUEST, "key")
        with self.assertRaisesRegex(ValueError, "different payload"):
            service.tools().create(replace(REQUEST, subject="Different intent payload"), "key")
        self.assertEqual(len(service.snapshot().effects), 1)

    def test_key_is_scoped_to_tenant(self):
        service = TicketService(Contract.IDEMPOTENT, Fault.NONE)
        first = service.tools().create(REQUEST, "key")
        second = service.tools().create(replace(REQUEST, tenant="another-synthetic-tenant"), "key")
        self.assertNotEqual(first.id, second.id)

    def test_oracle_rejects_success_without_commit_or_for_wrong_ticket(self):
        report = Report("success_observed", "ticket-1", (Observation("success", "ticket-1"),))
        for effects in ((), (Ticket("ticket-1", replace(REQUEST, intent="other-intent")),)):
            with self.subTest(effects=effects):
                score = evaluate(REQUEST, report, Snapshot(effects, 1))
                self.assertTrue(score["false_success"])
                self.assertFalse(score["safe_verified_completion"])

    def test_oracle_rejects_incorrect_reported_ticket_id(self):
        report = Report("success_observed", "invented-id", ())
        score = evaluate(REQUEST, report, Snapshot((Ticket("ticket-1", REQUEST),), 1))
        self.assertTrue(score["false_success"])

    def test_repeated_runs_are_identical(self):
        self.assertEqual(run_fixtures(), run_fixtures())


if __name__ == "__main__":
    unittest.main()
