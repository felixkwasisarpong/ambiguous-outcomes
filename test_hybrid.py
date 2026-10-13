from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

import comparison
from comparison3 import ROOT, TARGETS, gates, run_case, specification
from durable import Contract, Fault, Journal, Service
from hybrid import hybrid
from smoke import SYNTHETIC_REQUEST


class HybridTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = {c["id"]: c for c in json.loads((ROOT / "comparison-v2.json").read_text())["cases"]}
        cls.rows = {name: run_case(cls.cases[name]) for name in TARGETS}

    def test_all_five_disclosed_regressions_complete_safely(self):
        for name, row in self.rows.items():
            with self.subTest(case=name):
                self.assertTrue(row["eventual"]["safe_verified_completion"])
                self.assertEqual(row["eventual"]["excess_effects"], 0)
                self.assertFalse(row["eventual"]["false_success"])
                self.assertLessEqual(row["reserved_calls"], self.cases[name]["max_calls"])

    def test_readback_avoids_post_commit_crash_when_budget_is_tight(self):
        row = self.rows["comparison-v2-005"]
        self.assertEqual(row["reserved_calls"], 4)
        self.assertEqual(row["report"]["observations"][-1]["kind"], "lookup")
        self.assertEqual(sum(a["crash_reached"] for a in row["attempts"]), 0)

    def test_unknown_bound_does_not_stop_after_first_lost_reply(self):
        row = self.rows["eval-v1-140"]
        self.assertEqual(sum(a["crash_reached"] for a in row["attempts"]), 2)
        self.assertEqual(row["controller_stopped_at"], 0)
        self.assertEqual(row["report"]["status"], "success_observed")

    def test_non_idempotent_recovery_still_matches_original_baseline(self):
        case = deepcopy(self.cases["comparison-v2-005"])
        case["contract"]["idempotent"] = False
        self.assertEqual(run_case(case), comparison.run_case(case, "baseline"))

    def test_unknown_pending_action_is_bounded_by_persistent_budget(self):
        case = deepcopy(self.cases["eval-v1-140"])
        case.update(crashes=[], max_calls=5)
        case["fault"].update(commit_delay=4, visibility_delay=3)
        row = run_case(case)
        self.assertEqual(row["report"]["status"], "budget_or_deadline_exhausted")
        self.assertEqual(row["reserved_calls"], 5)
        self.assertEqual(row["at_stop"]["committed_effects"], 0)
        self.assertEqual(row["eventual"]["committed_effects"], 1)
        self.assertFalse(row["eventual"]["false_success"])

    def test_expired_deadline_cannot_start_action(self):
        case = deepcopy(self.cases["comparison-v2-005"])
        case.update(deadline=0, crashes=[])
        row = run_case(case)
        self.assertEqual(row["reserved_calls"], 0)
        self.assertEqual(row["eventual"]["committed_effects"], 0)

    def test_original_submission_time_and_key_survive_replays(self):
        with tempfile.TemporaryDirectory() as folder:
            sp, jp = Path(folder) / "service.db", Path(folder) / "journal.db"
            Service.initialize(sp, Contract(True, True, 8), Fault(4, 2))
            service, journal = Service(sp), Journal(jp)
            try:
                original = journal.prepare("synthetic-operation", SYNTHETIC_REQUEST, service.contract, 20, 12)
                result = hybrid(journal, service.tools(), SYNTHETIC_REQUEST, deadline=20, max_calls=12)
                saved = journal.get("synthetic-operation")
                self.assertEqual(saved["op_key"], original["op_key"])
                self.assertEqual(saved["submitted_at"], 0)
                self.assertEqual(result.status, "success_observed")
                self.assertEqual(len(service.snapshot().effects), 1)
            finally:
                service.close()
                journal.close()

    def test_changed_payload_or_limits_cannot_reuse_saved_operation(self):
        with tempfile.TemporaryDirectory() as folder:
            sp, jp = Path(folder) / "service.db", Path(folder) / "journal.db"
            Service.initialize(sp)
            service, journal = Service(sp), Journal(jp)
            try:
                journal.prepare("synthetic-operation", SYNTHETIC_REQUEST, service.contract, 12, 12)
                wrong = hybrid(journal, service.tools(), replace(SYNTHETIC_REQUEST, subject="Different task"))
                inflated = hybrid(journal, service.tools(), SYNTHETIC_REQUEST, max_calls=20)
                self.assertEqual((wrong.status, inflated.status), ("rejected", "rejected"))
                self.assertEqual(service.snapshot().effects, ())
            finally:
                service.close()
                journal.close()


class ThreePolicyComparisonTests(unittest.TestCase):
    def test_fresh_spec_keeps_all_previous_cases_unchanged(self):
        spec = specification()
        self.assertEqual(spec, specification())
        previous = json.loads((ROOT / "comparison-v2.json").read_text())["cases"]
        self.assertEqual(spec["cases"][:216], previous)
        self.assertEqual(len(spec["cases"]), 288)
        self.assertEqual(len({c["id"] for c in spec["cases"]}), 288)
        self.assertEqual(sum(c["group"] == "v3_new_stress" for c in spec["cases"]), 72)

    def test_no_regression_gate_catches_loss_even_when_safety_passes(self):
        prior = json.loads((ROOT / "comparison-results.json").read_text())
        cases = json.loads((ROOT / "comparison-v2.json").read_text())["cases"]
        old = prior["baseline"]
        new = deepcopy(old)
        successful = next(r for r in new if r["eventual"]["safe_verified_completion"])
        successful["eventual"]["safe_verified_completion"] = False
        checks, _ = gates(cases, {"baseline": old, "key_first": old, "hybrid": new},
                          {"baseline": {"accepted": True}, "key_first": {"accepted": True}, "hybrid": {"accepted": True}})
        self.assertTrue(checks["safety_and_controls"])
        self.assertFalse(checks["no_regression_goal_met"])
        self.assertIn(successful["id"], checks["new_losses"]["baseline"])


if __name__ == "__main__":
    unittest.main()
