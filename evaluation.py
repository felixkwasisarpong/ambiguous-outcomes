"""Frozen, synthetic evaluation of the unchanged durable recovery controller.

This is separate from development fixtures, not an externally authored benchmark.
No model calls, secrets, network access, or real customer data.
"""

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import random
import tempfile

from durable import CRASH_EXIT, Contract, Fault, Journal, Service, launch
from smoke import SYNTHETIC_REQUEST


ROOT = Path(__file__).resolve().parent
SEED = 20261013
CASES_FILE = "evaluation-v1.json"
LOCK_FILE = "evaluation-v1.lock.json"
FROZEN_FILES = (
    CASES_FILE, "evaluation.py", "test_evaluation.py", "EVALUATION.md",
    "durable.py", "smoke.py",
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_cases():
    """Declare cases without running or consulting controller outcomes."""
    rng = random.Random(SEED)
    cases = []

    def add(group, contract, fault, crashes, deadline, budget, must_complete=False):
        cases.append({
            "id": f"eval-v1-{len(cases) + 1:03d}", "group": group,
            "contract": asdict(contract), "fault": asdict(fault),
            "crashes": crashes, "deadline": deadline, "max_calls": budget,
            "must_complete": must_complete,
        })

    # High-resource cases have a truthful bound and a usable recovery mechanism.
    # Completion is specified before execution, rather than inferred from status.
    for contract in (Contract(True, True, 9), Contract(True, False, 9), Contract(False, True, 9)):
        for fault in (Fault(lose_reply=False), Fault(4, 3), Fault(0, 6), Fault(drop_request=True)):
            for crashes in ([], ["during_confirmation", "during_confirmation"]):
                add("completion_control", contract, fault, crashes, 128, 32, True)

    # Balanced contract families; budgets and deadlines deliberately constrain
    # some cases. No aggregate completion threshold is imposed on this group.
    families = [(True, True, True), (True, False, True), (False, True, True),
                (False, False, True), (False, True, False), (True, True, False)]
    schedules = [[], ["before_prepare"] * 2, ["before_dispatch"] * 2,
                 ["during_service_commit"] * 2, ["after_service_commit"] * 2,
                 ["after_dispatch"] * 2, ["during_confirmation"] * 2,
                 ["after_confirmation"]]
    for idempotent, lookup, bounded in families:
        for mode in ("clean", "reply_lost", "delayed", "stale", "dropped"):
            for budget in (1, 3, 7, 12):
                delay = rng.choice([1, 2, 4, 6, 8]) if mode == "delayed" else 0
                visibility = rng.choice([1, 3, 5, 7]) if mode in ("delayed", "stale") else 0
                contract = Contract(idempotent, lookup, delay + visibility + rng.choice([0, 1, 4]) if bounded else None)
                fault = Fault(delay, visibility, mode == "dropped", mode != "clean")
                add("stress", contract, fault, list(rng.choice(schedules)),
                    rng.choice([0, 1, 2, 6, 11, 23]), budget)
    return {
        "schema_version": 1, "seed": SEED,
        "scope": "Separately specified synthetic cases; same simulator and author as development",
        "acceptance": {
            "max_excess_effects": 0, "max_wrong_effects": 0,
            "max_false_successes": 0, "max_budget_violations": 0,
            "max_execution_errors": 0, "require_all_completion_controls": True,
        },
        "cases": cases,
    }


def freeze(root=ROOT):
    case_path, lock_path = root / CASES_FILE, root / LOCK_FILE
    if case_path.exists() or lock_path.exists():
        raise FileExistsError("Version 1 is immutable; use a new version for changes")
    case_path.write_text(json.dumps(build_cases(), indent=2, sort_keys=True) + "\n")
    lock = {
        "schema_version": 1,
        "source_sha256": {name: digest(root / name) for name in FROZEN_FILES},
        "rule": "Freeze before baseline execution; changes require a new evaluation version",
    }
    lock_path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
    return lock


def verify_lock(root=ROOT):
    lock = json.loads((root / LOCK_FILE).read_text())
    if set(lock["source_sha256"]) != set(FROZEN_FILES):
        raise ValueError("Unexpected frozen file list")
    for name, expected in lock["source_sha256"].items():
        if digest(root / name) != expected:
            raise ValueError(f"Frozen file changed: {name}")
    return lock


def score(report, ledger, request):
    """Independent implementation: deliberately does not call smoke.evaluate."""
    matching = [ticket for ticket in ledger if ticket["request"] == request]
    wrong = len(ledger) - len(matching)
    exact = (len(matching) == 1 and wrong == 0
             and report["ticket_id"] == matching[0]["id"])
    claims_success = report["status"] == "success_observed"
    return {
        "committed_effects": len(ledger),
        "excess_effects": max(0, len(matching) - 1),
        "wrong_effects": wrong,
        "safe_verified_completion": claims_success and exact,
        "false_success": claims_success and not exact,
    }


def run_case(case):
    attempts = []
    report = {"status": "execution_error", "ticket_id": None, "observations": []}
    execution_error = None
    with tempfile.TemporaryDirectory() as directory:
        service_path, journal_path = Path(directory) / "service.db", Path(directory) / "journal.db"
        Service.initialize(service_path, Contract(**case["contract"]), Fault(**case["fault"]))
        try:
            # An unreached injection is recorded, never counted as an actual crash.
            # If the controller returns normally, stop instead of restarting a
            # completed or resource-exhausted episode to chase the target hook.
            for point in case["crashes"] + [""]:
                result = launch(service_path, journal_path, point, case["deadline"], case["max_calls"])
                reached = result.returncode == CRASH_EXIT
                attempts.append({"requested_crash": point or None, "exit_code": result.returncode,
                                 "crash_reached": reached})
                if reached and point:
                    continue
                if result.returncode:
                    raise RuntimeError("Worker failed")
                report = json.loads(result.stdout)
                break
        except Exception as error:
            # Do not publish local temporary paths from child tracebacks.
            execution_error = type(error).__name__
        service, journal = Service(service_path), Journal(journal_path)
        try:
            stopped_at = service.now()
            before = service.snapshot()
            before_ledger = [asdict(t) for t in before.effects]
            service.drain()
            final = service.snapshot()
            ledger = [asdict(t) for t in final.effects]
            operation = journal.get("synthetic-operation")
            reserved = operation["used"] if operation else 0
            return {
                "id": case["id"], "group": case["group"], "must_complete": case["must_complete"],
                "report": report, "attempts": attempts, "execution_error": execution_error,
                "controller_stopped_at": stopped_at, "reserved_calls": reserved,
                "service_committed_call_counter": final.calls,
                "budget_violation": reserved > case["max_calls"],
                "at_stop": score(report, before_ledger, asdict(SYNTHETIC_REQUEST)),
                "eventual": score(report, ledger, asdict(SYNTHETIC_REQUEST)),
                "committed_effects_ledger": ledger,
            }
        finally:
            service.close()
            journal.close()


def summarize(rows, acceptance):
    checks = {
        "excess_effects": sum(r["eventual"]["excess_effects"] for r in rows),
        "wrong_effects": sum(r["eventual"]["wrong_effects"] for r in rows),
        "false_successes": sum(r["eventual"]["false_success"] for r in rows),
        "budget_violations": sum(r["budget_violation"] for r in rows),
        "execution_errors": sum(r["execution_error"] is not None for r in rows),
    }
    controls = [r for r in rows if r["must_complete"]]
    missed = [r["id"] for r in controls if not r["eventual"]["safe_verified_completion"]]
    groups = {}
    for group in sorted({r["group"] for r in rows}):
        selected = [r for r in rows if r["group"] == group]
        groups[group] = {
            "cases": len(selected),
            "verified_completions": sum(r["eventual"]["safe_verified_completion"] for r in selected),
            "statuses": dict(sorted(Counter(r["report"]["status"] for r in selected).items())),
            "reserved_calls_total": sum(r["reserved_calls"] for r in selected),
        }
    gates = {name: value <= acceptance["max_" + name] for name, value in checks.items()}
    gates["completion_controls"] = not missed if acceptance["require_all_completion_controls"] else True
    return {
        "cases": len(rows), "counts": checks, "gates": gates,
        "accepted": all(gates.values()), "missed_completion_controls": missed,
        "actual_process_crashes": sum(a["crash_reached"] for r in rows for a in r["attempts"]),
        "cases_with_repeated_crashes": sum(sum(a["crash_reached"] for a in r["attempts"]) >= 2 for r in rows),
        "unreached_crash_requests": sum(bool(a["requested_crash"]) and not a["crash_reached"] for r in rows for a in r["attempts"]),
        "groups": groups,
    }


def run_evaluation():
    lock = verify_lock()
    spec = json.loads((ROOT / CASES_FILE).read_text())
    rows = [run_case(case) for case in spec["cases"]]
    return {
        "schema_version": 1, "evaluation": "v1", "seed": spec["seed"],
        "lock_sha256": digest(ROOT / LOCK_FILE), "frozen_source_sha256": lock["source_sha256"],
        "external_api_calls": 0, "model_calls": 0,
        "summary": summarize(rows, spec["acceptance"]), "cases": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--freeze", action="store_true")
    args = parser.parse_args()
    output = freeze() if args.freeze else run_evaluation()
    print(json.dumps(output, indent=2, sort_keys=True))
    if not args.freeze and not output["summary"]["accepted"]:
        raise SystemExit(1)
