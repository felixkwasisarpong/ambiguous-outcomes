from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest

from evaluation import (CASES_FILE, FROZEN_FILES, LOCK_FILE, ROOT, build_cases,
                        freeze, run_case, score, summarize, verify_lock)
from smoke import SYNTHETIC_REQUEST


class EvaluationSpecificationTests(unittest.TestCase):
    def test_generation_is_deterministic_and_has_unique_cases(self):
        spec = build_cases()
        self.assertEqual(spec, build_cases())
        self.assertEqual(len(spec["cases"]), 144)
        self.assertEqual(len({c["id"] for c in spec["cases"]}), 144)
        self.assertEqual(sum(c["must_complete"] for c in spec["cases"]), 24)

    def test_all_generated_contract_bounds_are_truthful(self):
        for case in build_cases()["cases"]:
            bound = case["contract"]["settlement_bound"]
            if bound is not None:
                self.assertLessEqual(case["fault"]["commit_delay"] + case["fault"]["visibility_delay"], bound)

    def test_freeze_refuses_overwrite_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in FROZEN_FILES:
                if name != CASES_FILE:
                    (root / name).write_text("synthetic fixture")
            freeze(root)
            verify_lock(root)
            with self.assertRaises(FileExistsError):
                freeze(root)
            (root / CASES_FILE).write_text("changed")
            with self.assertRaisesRegex(ValueError, "Frozen file changed"):
                verify_lock(root)

    def test_checked_in_spec_matches_generator_and_lock_when_present(self):
        if not (ROOT / LOCK_FILE).exists():
            self.skipTest("Evaluation not yet frozen")
        self.assertEqual(json.loads((ROOT / CASES_FILE).read_text()), build_cases())
        verify_lock()


class IndependentScorerTests(unittest.TestCase):
    def setUp(self):
        self.request = asdict(SYNTHETIC_REQUEST)
        self.ticket = {"id": "ticket-1", "request": self.request}
        self.success = {"status": "success_observed", "ticket_id": "ticket-1"}

    def test_single_matching_effect_is_verified(self):
        self.assertTrue(score(self.success, [self.ticket], self.request)["safe_verified_completion"])

    def test_success_with_no_effect_duplicate_wrong_id_or_wrong_request_fails(self):
        wrong = {"id": "ticket-1", "request": {**self.request, "tenant": "another-tenant"}}
        for ledger in [[], [self.ticket, self.ticket], [wrong], [{**self.ticket, "id": "other"}]]:
            with self.subTest(ledger=ledger):
                result = score(self.success, ledger, self.request)
                self.assertTrue(result["false_success"])
                self.assertFalse(result["safe_verified_completion"])

    def test_unresolved_committed_action_is_not_a_verified_completion(self):
        report = {"status": "unresolved", "ticket_id": None}
        result = score(report, [self.ticket], self.request)
        self.assertFalse(result["safe_verified_completion"])
        self.assertFalse(result["false_success"])


class EvaluationRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = build_cases()
        # Few integration controls; the full frozen evaluation is a separate run.
        cls.clean = run_case(cls.spec["cases"][0])
        cls.repeated = run_case(cls.spec["cases"][1])

    def test_repeated_crashes_are_actually_reached(self):
        self.assertEqual(sum(a["crash_reached"] for a in self.repeated["attempts"]), 2)
        self.assertTrue(self.repeated["eventual"]["safe_verified_completion"])

    def test_unreached_injection_is_not_counted_as_a_crash(self):
        case = deepcopy(self.spec["cases"][0])
        case.update(deadline=0, crashes=["before_dispatch"])
        result = run_case(case)
        self.assertFalse(result["attempts"][0]["crash_reached"])
        self.assertEqual(result["report"]["status"], "budget_or_deadline_exhausted")

    def test_each_acceptance_gate_rejects_a_corresponding_failure(self):
        for field in ["excess_effects", "wrong_effects", "false_success"]:
            row = deepcopy(self.clean)
            row["eventual"][field] = 1
            self.assertFalse(summarize([row], self.spec["acceptance"])["accepted"])
        for field, value in [("budget_violation", True), ("execution_error", "RuntimeError")]:
            row = deepcopy(self.clean)
            row[field] = value
            self.assertFalse(summarize([row], self.spec["acceptance"])["accepted"])
        row = deepcopy(self.clean)
        row["eventual"]["safe_verified_completion"] = False
        self.assertFalse(summarize([row], self.spec["acceptance"])["accepted"])


if __name__ == "__main__":
    unittest.main()
