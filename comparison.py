"""Paired deterministic policy comparison; no model or network calls."""
import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
from unittest.mock import patch

import evaluation
from durable import CRASH_EXIT, Journal, Service, recover
from smoke import SYNTHETIC_REQUEST
from strategies import key_first

ROOT = Path(__file__).resolve().parent
FILES = ("comparison-v2.json", "COMPARISON.md", "comparison.py", "strategies.py",
         "test_comparison.py", "evaluation-v1.lock.json", "evaluation-results.json")


def specification():
    old = json.loads((ROOT / "evaluation-v1.json").read_text())
    cases = old["cases"]
    rng = random.Random(20261014)
    families = [(True, True, True), (True, False, True), (False, True, True),
                (False, False, True), (False, True, False), (True, True, False)]
    for keyed, lookup, bounded in families:
        for mode in ("reply_lost", "delayed", "stale", "dropped"):
            for budget in (2, 4, 8):
                delay = rng.choice([2, 5, 7]) if mode == "delayed" else 0
                visibility = rng.choice([2, 4, 6]) if mode in ("delayed", "stale") else 0
                cases.append({
                    "id": f"comparison-v2-{len(cases)-143:03d}", "group": "new_stress",
                    "contract": {"idempotent": keyed, "lookup": lookup,
                                 "settlement_bound": delay + visibility + 2 if bounded else None},
                    "fault": {"commit_delay": delay, "visibility_delay": visibility,
                              "drop_request": mode == "dropped", "lose_reply": True},
                    "crashes": list(rng.choice([[], [], ["before_dispatch"] * 2,
                                                 ["after_service_commit"] * 2,
                                                 ["during_confirmation"] * 2])),
                    "deadline": rng.choice([2, 5, 12, 30]), "max_calls": budget,
                    "must_complete": False,
                })
    return {"version": 2, "new_seed": 20261014, "acceptance": old["acceptance"], "cases": cases}


def freeze():
    paths = [ROOT / "comparison-v2.json", ROOT / "comparison-v2.lock.json"]
    if any(p.exists() for p in paths):
        raise FileExistsError("Comparison v2 already frozen")
    evaluation.verify_lock()
    paths[0].write_text(json.dumps(specification(), indent=2, sort_keys=True) + "\n")
    lock = {"source_sha256": {name: evaluation.digest(ROOT / name) for name in FILES}}
    paths[1].write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
    return lock


def verify():
    evaluation.verify_lock()
    lock = json.loads((ROOT / "comparison-v2.lock.json").read_text())
    if set(lock["source_sha256"]) != set(FILES):
        raise ValueError("Unexpected file list")
    for name, sha in lock["source_sha256"].items():
        if evaluation.digest(ROOT / name) != sha:
            raise ValueError(f"Frozen comparison file changed: {name}")
    return lock


def launch_policy(policy, service, journal, point="", deadline=12, max_calls=12):
    return subprocess.run([sys.executable, "-B", str(ROOT / "comparison.py"),
        "--worker", policy, str(service), str(journal), "--crash", point,
        "--deadline", str(deadline), "--max-calls", str(max_calls)],
        capture_output=True, text=True, timeout=15)


def run_case(case, policy):
    # Adapter changes only the worker executable, not the frozen service, case
    # runner, budget accounting, crash scheduling, scorer or final drain.
    with patch.object(evaluation, "launch", lambda *args: launch_policy(policy, *args)):
        return evaluation.run_case(case)


def worker(policy, service_path, journal_path, point, deadline, max_calls):
    def crash(actual):
        if actual == point:
            os._exit(CRASH_EXIT)
    service, journal = Service(service_path, crash), Journal(journal_path, crash)
    try:
        controller = {"baseline": recover, "key_first": key_first}[policy]
        return asdict(controller(journal, service.tools(), SYNTHETIC_REQUEST,
                                 deadline=deadline, max_calls=max_calls, crash=crash))
    finally:
        service.close()
        journal.close()


def diagnose_v1(cases, rows):
    """Observable stopping condition, not a proof that completion was impossible."""
    spec = {c["id"]: c for c in cases}
    diagnoses = []
    for row in rows:
        if row["eventual"]["safe_verified_completion"]:
            continue
        case = spec[row["id"]]
        deadline = row["controller_stopped_at"] >= case["deadline"]
        budget = row["reserved_calls"] >= case["max_calls"]
        primary = ("deadline_reached" if deadline else "budget_used" if budget else "policy_abstained")
        diagnoses.append({"id": row["id"], "primary_observed_stop": primary,
                          "deadline_reached": deadline, "budget_used": budget,
                          "idempotent": case["contract"]["idempotent"],
                          "original_status": row["report"]["status"]})
    return {"counts": dict(sorted(Counter(r["primary_observed_stop"] for r in diagnoses).items())),
            "cases": diagnoses}


def paired(cases, baseline, candidate):
    if not (len(cases) == len(baseline) == len(candidate)):
        raise ValueError("Incomplete paired results")
    pairs = []
    for case, old, new in zip(cases, baseline, candidate):
        assert case["id"] == old["id"] == new["id"]
        old_ok, new_ok = old["eventual"]["safe_verified_completion"], new["eventual"]["safe_verified_completion"]
        pairs.append({"id": case["id"], "group": case["group"],
                      "outcome": "gain" if new_ok and not old_ok else "loss" if old_ok and not new_ok else "tie",
                      "reserved_call_delta": new["reserved_calls"] - old["reserved_calls"],
                      "virtual_time_delta": new["controller_stopped_at"] - old["controller_stopped_at"],
                      "requested_crashes": len(case["crashes"]),
                      "baseline_actual_crashes": sum(a["crash_reached"] for a in old["attempts"]),
                      "candidate_actual_crashes": sum(a["crash_reached"] for a in new["attempts"])})
    groups = {}
    for label, selected in [("all", pairs), ("no_requested_crashes", [p for p in pairs if not p["requested_crashes"]]),
                            ("requested_crashes", [p for p in pairs if p["requested_crashes"]])]:
        groups[label] = {"cases": len(selected), "outcomes": dict(sorted(Counter(p["outcome"] for p in selected).items())),
                         "reserved_call_delta": sum(p["reserved_call_delta"] for p in selected)}
    return {"groups": groups, "pairs": pairs}


def run():
    lock = verify()
    spec = json.loads((ROOT / "comparison-v2.json").read_text())
    baseline = [run_case(c, "baseline") for c in spec["cases"]]
    candidate = [run_case(c, "key_first") for c in spec["cases"]]
    original = json.loads((ROOT / "evaluation-results.json").read_text())["cases"]
    reproduction = baseline[:len(original)] == original
    summaries = {"baseline": evaluation.summarize(baseline, spec["acceptance"]),
                 "key_first": evaluation.summarize(candidate, spec["acceptance"])}
    return {"version": 2, "frozen_sha256": lock["source_sha256"],
            "lock_sha256": evaluation.digest(ROOT / "comparison-v2.lock.json"),
            "model_calls": 0, "external_api_calls": 0,
            "v1_baseline_reproduced": reproduction,
            "accepted": reproduction and all(s["accepted"] for s in summaries.values()),
            "v1_diagnosis": diagnose_v1(spec["cases"], original),
            "summaries": summaries, "comparison": paired(spec["cases"], baseline, candidate),
            "baseline": baseline, "key_first": candidate}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--worker", nargs=3)
    parser.add_argument("--crash", default="")
    parser.add_argument("--deadline", type=int, default=12)
    parser.add_argument("--max-calls", type=int, default=12)
    args = parser.parse_args()
    result = (worker(*args.worker, args.crash, args.deadline, args.max_calls) if args.worker else
              freeze() if args.freeze else run())
    print(json.dumps(result, indent=2, sort_keys=True))
    if not args.worker and not args.freeze and not result["accepted"]:
        raise SystemExit(1)
