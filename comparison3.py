"""Frozen three-policy study; prior results reused with explicit provenance."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import random
import subprocess
import sys
from unittest.mock import patch

import comparison
import evaluation
from durable import CRASH_EXIT, Journal, Service
from hybrid import hybrid
from smoke import SYNTHETIC_REQUEST

ROOT = Path(__file__).resolve().parent
FILES = ("comparison-v3.json", "COMPARISON_V3.md", "comparison3.py", "hybrid.py",
         "test_hybrid.py", "comparison-v2.lock.json", "comparison-results.json")
TARGETS = ["eval-v1-132", "eval-v1-140", "comparison-v2-005",
           "comparison-v2-063", "comparison-v2-069"]


def specification():
    old = json.loads((ROOT / "comparison-v2.json").read_text())
    cases = old["cases"]
    rng = random.Random(20261015)
    families = [(True, True, True), (True, False, True), (False, True, True),
                (False, False, True), (False, True, False), (True, True, False)]
    for keyed, lookup, bounded in families:
        for mode in ("reply_lost", "delayed", "stale", "dropped"):
            for budget in (3, 5, 9):
                delay = rng.choice([1, 3, 6]) if mode == "delayed" else 0
                visibility = rng.choice([1, 5, 8]) if mode in ("delayed", "stale") else 0
                cases.append({
                    "id": f"comparison-v3-{len(cases)-215:03d}", "group": "v3_new_stress",
                    "contract": {"idempotent": keyed, "lookup": lookup,
                                 "settlement_bound": delay + visibility + 1 if bounded else None},
                    "fault": {"commit_delay": delay, "visibility_delay": visibility,
                              "drop_request": mode == "dropped", "lose_reply": True},
                    "crashes": list(rng.choice([[], [], ["before_dispatch"] * 2,
                                                ["after_service_commit"] * 2,
                                                ["during_confirmation"] * 2])),
                    "deadline": rng.choice([1, 4, 10, 25]), "max_calls": budget,
                    "must_complete": False,
                })
    return {"version": 3, "new_seed": 20261015, "acceptance": old["acceptance"],
            "targets": TARGETS, "cases": cases}


def freeze():
    paths = [ROOT / "comparison-v3.json", ROOT / "comparison-v3.lock.json"]
    if any(p.exists() for p in paths):
        raise FileExistsError("Comparison v3 already frozen")
    comparison.verify()
    paths[0].write_text(json.dumps(specification(), indent=2, sort_keys=True) + "\n")
    lock = {"source_sha256": {name: evaluation.digest(ROOT / name) for name in FILES}}
    paths[1].write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
    return lock


def verify():
    comparison.verify()
    lock = json.loads((ROOT / "comparison-v3.lock.json").read_text())
    if set(lock["source_sha256"]) != set(FILES):
        raise ValueError("Unexpected frozen file list")
    for name, sha in lock["source_sha256"].items():
        if evaluation.digest(ROOT / name) != sha:
            raise ValueError(f"Frozen comparison file changed: {name}")
    return lock


def launch_hybrid(service, journal, point="", deadline=12, max_calls=12):
    return subprocess.run([sys.executable, "-B", str(ROOT / "comparison3.py"),
        "--worker", str(service), str(journal), "--crash", point,
        "--deadline", str(deadline), "--max-calls", str(max_calls)],
        capture_output=True, text=True, timeout=15)


def run_case(case):
    with patch.object(evaluation, "launch", launch_hybrid):
        return evaluation.run_case(case)


def worker(service_path, journal_path, point, deadline, max_calls):
    def crash(actual):
        if actual == point:
            os._exit(CRASH_EXIT)
    service, journal = Service(service_path, crash), Journal(journal_path, crash)
    try:
        return asdict(hybrid(journal, service.tools(), SYNTHETIC_REQUEST,
                            deadline=deadline, max_calls=max_calls, crash=crash))
    finally:
        service.close()
        journal.close()


def gates(cases, policies, summaries):
    rows = {r["id"]: r for r in policies["hybrid"]}
    fixed = {case_id: rows[case_id]["eventual"]["safe_verified_completion"] for case_id in TARGETS}
    paired = {name: comparison.paired(cases, policies[name], policies["hybrid"])
              for name in ("baseline", "key_first")}
    losses = {name: [p["id"] for p in result["pairs"] if p["outcome"] == "loss"]
              for name, result in paired.items()}
    safety_and_controls = all(s["accepted"] for s in summaries.values())
    return {"safety_and_controls": safety_and_controls, "target_fixes": fixed,
            "new_losses": losses,
            "no_regression_goal_met": safety_and_controls and all(fixed.values()) and not any(losses.values())}, paired


def run():
    lock = verify()
    spec = json.loads((ROOT / "comparison-v3.json").read_text())
    prior = json.loads((ROOT / "comparison-results.json").read_text())
    old_cases = json.loads((ROOT / "comparison-v2.json").read_text())["cases"]
    old_count = len(old_cases)
    if spec["cases"][:old_count] != old_cases or not prior["accepted"]:
        raise ValueError("Cannot reuse unverified or mismatched prior results")
    policies = {}
    for policy in ("baseline", "key_first"):
        if [r["id"] for r in prior[policy]] != [c["id"] for c in old_cases]:
            raise ValueError("Incomplete prior result rows")
        fresh = [comparison.run_case(c, policy) for c in spec["cases"][old_count:]]
        policies[policy] = prior[policy] + fresh
        print(f"Completed {policy}: {len(fresh)} fresh cases; {old_count} locked prior rows reused", file=sys.stderr, flush=True)
    policies["hybrid"] = []
    for index, case in enumerate(spec["cases"], 1):
        policies["hybrid"].append(run_case(case))
        if index % 48 == 0:
            print(f"Completed hybrid: {index}/{len(spec['cases'])}", file=sys.stderr, flush=True)
    summaries = {name: evaluation.summarize(rows, spec["acceptance"]) for name, rows in policies.items()}
    checks, pairs = gates(spec["cases"], policies, summaries)
    return {"version": 3, "frozen_sha256": lock["source_sha256"],
            "lock_sha256": evaluation.digest(ROOT / "comparison-v3.lock.json"),
            "external_api_calls": 0, "model_calls": 0,
            "provenance": {"reused_cases_per_previous_policy": old_count,
                           "fresh_cases_per_previous_policy": len(spec["cases"]) - old_count,
                           "fresh_hybrid_cases": len(spec["cases"]),
                           "prior_result_sha256": evaluation.digest(ROOT / "comparison-results.json")},
            "gates": checks, "summaries": summaries, "comparisons": pairs, "policies": policies}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--worker", nargs=2)
    parser.add_argument("--crash", default="")
    parser.add_argument("--deadline", type=int, default=12)
    parser.add_argument("--max-calls", type=int, default=12)
    args = parser.parse_args()
    output = (worker(*args.worker, args.crash, args.deadline, args.max_calls) if args.worker else
              freeze() if args.freeze else run())
    print(json.dumps(output, indent=2, sort_keys=True))
    if not args.worker and not args.freeze and not output["gates"]["no_regression_goal_met"]:
        raise SystemExit(1)
