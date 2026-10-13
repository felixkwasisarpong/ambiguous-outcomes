"""End-to-end process exit tests against persistent, independent state stores."""

from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from durable import (CRASH_EXIT, Contract, Fault, Journal, Service, launch, recover, run_fixtures)
from smoke import SYNTHETIC_REQUEST as REQUEST, evaluate


class CrashMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = run_fixtures()
        cls.rows = {row["fixture"]: row for row in cls.manifest["fixtures"]}

    def test_all_crash_points_reached_in_child_processes(self):
        self.assertEqual(len(self.rows), 14)
        self.assertTrue(all(row["crash_exit"] == CRASH_EXIT for row in self.rows.values()))

    def test_recovery_never_duplicates_or_falsely_reports_success(self):
        for name, row in self.rows.items():
            with self.subTest(fixture=name):
                self.assertEqual(row["eventual"]["committed_effects"], 1)
                self.assertEqual(row["eventual"]["excess_effects"], 0)
                self.assertFalse(row["eventual"]["false_success"])
                self.assertLessEqual(row["reserved_calls"], 12)

    def test_supported_contracts_complete(self):
        for name, row in self.rows.items():
            with self.subTest(fixture=name):
                self.assertEqual(row["eventual"]["safe_verified_completion"], name != "restart_unknown_opaque")

    def test_unknown_bound_preserves_ambiguity(self):
        row = self.rows["restart_unknown_opaque"]
        self.assertEqual(row["report"]["status"], "unresolved")
        self.assertEqual(row["at_stop"]["committed_effects"], 0)
        self.assertEqual(row["eventual"]["committed_effects"], 1)

    def test_confirmed_operation_does_not_call_service_again(self):
        row = self.rows["after_confirmation"]
        self.assertEqual(row["eventual"]["tool_calls"], 1)
        self.assertEqual(row["reserved_calls"], 1)
        self.assertEqual(row["report"]["observations"][0]["kind"], "journal")

    def test_repeated_process_runs_produce_same_manifest(self):
        self.assertEqual(self.manifest, run_fixtures())


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service_path = Path(self.temp.name) / "service.db"
        self.journal_path = Path(self.temp.name) / "journal.db"

    def initialize(self, contract=Contract(), fault=Fault()):
        Service.initialize(self.service_path, contract, fault)

    def open(self):
        service, journal = Service(self.service_path), Journal(self.journal_path)
        self.addCleanup(service.close)
        self.addCleanup(journal.close)
        return service, journal

    def crash(self, point, **kwargs):
        result = launch(self.service_path, self.journal_path, point, **kwargs)
        self.assertEqual(result.returncode, CRASH_EXIT, result.stderr)

    def resume(self, **kwargs):
        result = launch(self.service_path, self.journal_path, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_same_operation_key_survives_repeated_crashes(self):
        self.initialize(Contract(True, False, None))
        self.crash("after_service_commit")
        service, journal = self.open()
        key = journal.get("synthetic-operation")["op_key"]
        self.crash("after_service_commit")
        self.assertEqual(journal.get("synthetic-operation")["op_key"], key)
        self.assertEqual(self.resume()["status"], "success_observed")
        self.assertEqual(len(service.snapshot().effects), 1)

    def test_budget_cannot_be_reset_by_restarting(self):
        self.initialize(Contract(True, False, None))
        self.crash("before_dispatch", max_calls=1)
        service, journal = self.open()
        for _ in range(2):
            self.assertEqual(self.resume(max_calls=1)["status"], "budget_or_deadline_exhausted")
        self.assertEqual(journal.get("synthetic-operation")["used"], 1)
        self.assertEqual(service.snapshot().calls, 0)
        self.assertEqual(service.snapshot().effects, ())

    def test_cannot_increase_budget_or_deadline_on_resume(self):
        self.initialize()
        self.crash("after_prepare", max_calls=1, deadline=2)
        self.assertEqual(self.resume(max_calls=12, deadline=12)["status"], "rejected")
        service, _ = self.open()
        self.assertEqual(service.snapshot().calls, 0)

    def test_deadline_exhaustion_does_not_erase_pending_commit(self):
        self.initialize(Contract(False, True, 5), Fault(3, 2))
        self.crash("after_dispatch", deadline=2)
        self.assertEqual(self.resume(deadline=2)["status"], "budget_or_deadline_exhausted")
        service, _ = self.open()
        self.assertEqual(service.now(), 2)
        self.assertEqual(service.snapshot().effects, ())
        service.drain()
        self.assertEqual(len(service.snapshot().effects), 1)
        self.assertEqual(self.resume(deadline=2)["status"], "budget_or_deadline_exhausted")
        self.assertEqual(len(service.snapshot().effects), 1)

    def test_wrong_payload_cannot_reuse_persisted_identity(self):
        self.initialize()
        self.crash("after_prepare")
        service, journal = self.open()
        result = recover(journal, service.tools(), replace(REQUEST, subject="Other fictional task"))
        self.assertEqual(result.status, "rejected")
        self.assertEqual(service.snapshot().calls, 0)

    def test_changed_contract_rejected_before_service_call(self):
        self.initialize()
        self.crash("after_prepare")
        service, journal = self.open()
        tools = replace(service.tools(), contract=Contract(False, False, None))
        self.assertEqual(recover(journal, tools, REQUEST).status, "rejected")
        self.assertEqual(service.snapshot().calls, 0)

    def test_unauthorized_tenant_has_no_effect(self):
        self.initialize()
        service, journal = self.open()
        result = recover(journal, service.tools(), replace(REQUEST, tenant="unapproved"))
        self.assertEqual(result.status, "rejected")
        self.assertEqual(service.snapshot().effects, ())

    def test_service_rejects_key_reuse_with_different_payload_after_reopen(self):
        self.initialize()
        self.crash("after_service_commit")
        service, journal = self.open()
        with self.assertRaisesRegex(ValueError, "payload mismatch"):
            service.create(replace(REQUEST, subject="Different task"), journal.get("synthetic-operation")["op_key"])
        self.assertEqual(len(service.snapshot().effects), 1)

    def test_existing_service_cannot_be_reinitialized(self):
        self.initialize()
        self.crash("after_service_commit")
        with self.assertRaises(sqlite3.OperationalError):
            self.initialize()
        service, _ = self.open()
        self.assertEqual(len(service.snapshot().effects), 1)

    def test_negative_control_fresh_journal_duplicates_opaque_action(self):
        self.initialize(Contract(False, True, 5))
        self.crash("after_service_commit")
        service = Service(self.service_path)
        fresh = Journal(Path(self.temp.name) / "forgotten.db")
        self.addCleanup(service.close)
        self.addCleanup(fresh.close)
        result = recover(fresh, service.tools(), REQUEST)
        score = evaluate(REQUEST, result, service.snapshot())
        self.assertEqual(score["excess_effects"], 1)
        self.assertTrue(score["false_success"])

    def test_negative_control_new_identity_duplicates_keyed_action(self):
        self.initialize(Contract(True, False, None))
        self.crash("after_service_commit")
        service, journal = self.open()
        result = recover(journal, service.tools(), REQUEST, operation="incorrect-new-operation")
        self.assertTrue(evaluate(REQUEST, result, service.snapshot())["false_success"])
        self.assertEqual(len(service.snapshot().effects), 2)

    def test_pending_idempotency_key_cannot_create_second_scheduled_action(self):
        self.initialize(Contract(True, False, 5), Fault(3, 2))
        self.crash("after_dispatch")
        service, journal = self.open()
        with self.assertRaises(TimeoutError):
            service.create(REQUEST, journal.get("synthetic-operation")["op_key"])
        service.drain()
        self.assertEqual(len(service.snapshot().effects), 1)

    def test_opaque_without_lookup_stops_even_with_bound(self):
        self.initialize(Contract(False, False, 5))
        self.crash("after_dispatch")
        self.assertEqual(self.resume()["status"], "unresolved")
        service, _ = self.open()
        self.assertEqual(len(service.snapshot().effects), 1)

    def test_zero_budget_prevents_initial_create(self):
        self.initialize()
        self.assertEqual(self.resume(max_calls=0)["status"], "budget_or_deadline_exhausted")
        service, _ = self.open()
        self.assertEqual(service.snapshot().calls, 0)

    def test_zero_deadline_prevents_initial_create(self):
        self.initialize()
        self.assertEqual(self.resume(deadline=0)["status"], "budget_or_deadline_exhausted")
        service, _ = self.open()
        self.assertEqual(service.snapshot().calls, 0)

    def test_interrupted_confirmation_transaction_rolls_back(self):
        self.initialize(fault=Fault(lose_reply=False))
        self.crash("during_confirmation")
        service, journal = self.open()
        self.assertIsNone(journal.get("synthetic-operation")["ticket"])
        self.assertEqual(len(service.snapshot().effects), 1)
        self.assertEqual(self.resume()["status"], "success_observed")
        self.assertEqual(len(service.snapshot().effects), 1)

    def test_interrupted_service_transaction_rolls_back_effect_and_key(self):
        self.initialize(fault=Fault(lose_reply=False))
        self.crash("during_service_commit")
        service, journal = self.open()
        self.assertEqual(service.snapshot().effects, ())
        self.assertEqual(journal.get("synthetic-operation")["used"], 1)
        self.assertEqual(self.resume()["status"], "success_observed")
        self.assertEqual(len(service.snapshot().effects), 1)

    def test_duplicate_visible_records_are_not_confirmed(self):
        self.initialize(Contract(False, True, 5), Fault(lose_reply=False))
        self.crash("after_service_commit")
        service, journal = self.open()
        service.create(REQUEST, None)
        self.assertEqual(recover(journal, service.tools(), REQUEST).status, "duplicate_detected")
        self.assertIsNone(journal.get("synthetic-operation")["ticket"])

    def test_corrupt_journal_does_not_start_service_action(self):
        self.initialize()
        self.journal_path.write_bytes(b"deliberately corrupt journal")
        result = launch(self.service_path, self.journal_path)
        self.assertNotEqual(result.returncode, 0)
        service = Service(self.service_path)
        self.addCleanup(service.close)
        self.assertEqual(service.snapshot().effects, ())


if __name__ == "__main__":
    unittest.main()
