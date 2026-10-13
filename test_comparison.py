from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from comparison import diagnose_v1, paired, run_case, specification
from durable import Contract, Fault, Journal, Service
from smoke import SYNTHETIC_REQUEST
from strategies import key_first


def fixture(**overrides):
    case = {"id": "comparison-unit", "group": "unit", "must_complete": False,
            "contract": {"idempotent": True, "lookup": True, "settlement_bound": 8},
            "fault": {"commit_delay": 0, "visibility_delay": 6, "drop_request": False, "lose_reply": True},
            "crashes": [], "deadline": 2, "max_calls": 2}
    case.update(overrides)
    return case


class PolicyTests(unittest.TestCase):
    def test_key_replay_recovers_stale_record_before_deadline(self):
        case = fixture()
        old, new = run_case(case, "baseline"), run_case(case, "key_first")
        self.assertFalse(old["eventual"]["safe_verified_completion"])
        self.assertTrue(new["eventual"]["safe_verified_completion"])
        self.assertEqual(new["reserved_calls"], 2)
        self.assertEqual(new["eventual"]["excess_effects"], 0)

    def test_non_idempotent_path_exactly_matches_baseline(self):
        case = fixture(contract={"idempotent": False, "lookup": True, "settlement_bound": 8},
                       max_calls=12, deadline=20)
        self.assertEqual(run_case(case, "baseline"), run_case(case, "key_first"))

    def test_repeated_confirmation_crashes_reuse_key_without_wait_reset(self):
        case = fixture(crashes=["during_confirmation"] * 2, max_calls=8)
        row = run_case(case, "key_first")
        self.assertEqual(sum(a["crash_reached"] for a in row["attempts"]), 2)
        self.assertTrue(row["eventual"]["safe_verified_completion"])
        self.assertEqual(row["controller_stopped_at"], 0)

    def test_pending_effect_remains_unique_after_wait(self):
        case = fixture(fault={"commit_delay": 4, "visibility_delay": 2,
                              "drop_request": False, "lose_reply": True}, deadline=20, max_calls=8)
        row = run_case(case, "key_first")
        self.assertTrue(row["eventual"]["safe_verified_completion"])
        self.assertEqual(row["eventual"]["committed_effects"], 1)
        self.assertEqual(row["controller_stopped_at"], 8)

    def test_unknown_bound_pending_effect_abstains(self):
        case = fixture(contract={"idempotent": True, "lookup": True, "settlement_bound": None},
                       fault={"commit_delay": 4, "visibility_delay": 2, "drop_request": False, "lose_reply": True},
                       deadline=20, max_calls=8)
        row = run_case(case, "key_first")
        self.assertEqual(row["report"]["status"], "unresolved")
        self.assertEqual(row["reserved_calls"], 2)
        self.assertEqual(row["eventual"]["committed_effects"], 1)
        self.assertFalse(row["eventual"]["false_success"])

    def test_budget_and_deadline_cannot_be_inflated(self):
        for case in [fixture(max_calls=1), fixture(deadline=0)]:
            row = run_case(case, "key_first")
            self.assertFalse(row["eventual"]["safe_verified_completion"])
            self.assertLessEqual(row["reserved_calls"], case["max_calls"])

    def test_payload_change_rejected_without_effect(self):
        with tempfile.TemporaryDirectory() as folder:
            Service.initialize(Path(folder) / "service.db", Contract(), Fault())
            service, journal = Service(Path(folder) / "service.db"), Journal(Path(folder) / "journal.db")
            try:
                journal.prepare("synthetic-operation", SYNTHETIC_REQUEST, Contract(), 12, 12)
                result = key_first(journal, service.tools(), replace(SYNTHETIC_REQUEST, subject="Different task"))
                self.assertEqual(result.status, "rejected")
                self.assertEqual(service.snapshot().effects, ())
            finally:
                service.close()
                journal.close()


class ComparisonTests(unittest.TestCase):
    def test_spec_is_deterministic_and_preserves_144_regression_cases(self):
        spec = specification()
        self.assertEqual(spec, specification())
        self.assertEqual(len(spec["cases"]), 216)
        self.assertEqual(len({c["id"] for c in spec["cases"]}), 216)
        self.assertEqual(sum(c["group"] == "new_stress" for c in spec["cases"]), 72)

    def test_paired_records_both_gains_and_losses(self):
        case = fixture()
        good = run_case(case, "key_first")
        bad = deepcopy(good)
        bad["eventual"]["safe_verified_completion"] = False
        self.assertEqual(paired([case], [bad], [good])["pairs"][0]["outcome"], "gain")
        self.assertEqual(paired([case], [good], [bad])["pairs"][0]["outcome"], "loss")
        with self.assertRaises(ValueError):
            paired([case], [], [good])

    def test_diagnosis_keeps_overlapping_budget_and_deadline_flags(self):
        case = fixture()
        row = run_case(case, "baseline")
        row["controller_stopped_at"] = case["deadline"]
        row["reserved_calls"] = case["max_calls"]
        diagnosis = diagnose_v1([case], [row])["cases"][0]
        self.assertTrue(diagnosis["deadline_reached"])
        self.assertTrue(diagnosis["budget_used"])
        self.assertEqual(diagnosis["primary_observed_stop"], "deadline_reached")


if __name__ == "__main__":
    unittest.main()
