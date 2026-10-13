"""Wall/CPU diagnostics alongside unchanged frozen evaluation results.

Targeted probes add unbuffered stage tracing. Full runs observe the original
worker commands from the parent only, keeping worker code and timeout unchanged.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import resource
import subprocess
import sys
import time
from unittest.mock import patch

import comparison
import comparison3
import evaluation

ROOT = Path(__file__).resolve().parent
TIMEOUT = 15
TARGETS = [("baseline", "eval-v1-064"), ("hybrid", "eval-v1-058"),
           ("hybrid", "comparison-v3-065"), ("hybrid", "comparison-v3-066")]


def cpu_children():
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def parse_events(stderr, parent_start_ns):
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    events = []
    for line in (stderr or "").splitlines():
        if line.startswith("RUNTIME_EVENT "):
            try:
                event = json.loads(line[len("RUNTIME_EVENT "):])
            except json.JSONDecodeError:
                events.append({"phase": "trace", "event": "truncated"})
                continue
            event["since_launch_ms"] = (event.pop("monotonic_ns") - parent_start_ns) / 1e6
            event["process_cpu_ms"] = event.pop("cpu_ns") / 1e6
            if "first_observable_ns" in event:
                event["first_observable_ms"] = (event.pop("first_observable_ns") - parent_start_ns) / 1e6
            events.append(event)
    return events


def pending_phases(events):
    stack = []
    for event in events:
        if event["event"] == "start":
            stack.append(event["phase"])
        elif event["event"] == "end":
            for i in range(len(stack) - 1, -1, -1):
                if stack[i] == event["phase"]:
                    del stack[i]
                    break
    return stack


def describe_command(command):
    name = Path(command[2]).name
    policy = "hybrid" if name == "comparison3.py" else command[command.index("--worker") + 1]
    return {"worker": name, "policy": policy, "crash": command[command.index("--crash") + 1] or None}


def observed_run(command, runner=subprocess.run, *, metadata=None, **kwargs):
    start, cpu = time.monotonic_ns(), cpu_children()
    record = {**(metadata or {}), "timeout_seconds": kwargs.get("timeout"), "timed_out": False}
    try:
        result = runner(command, **kwargs)
        record["returncode"] = result.returncode
        record["events"] = parse_events(result.stderr, start)
        return result, record
    except subprocess.TimeoutExpired as error:
        record.update(timed_out=True, returncode=None, events=parse_events(error.stderr, start))
        error.runtime_record = record
        raise
    finally:
        record["duration_ms"] = (time.monotonic_ns() - start) / 1e6
        record["child_cpu_ms"] = (cpu_children() - cpu) * 1000
        record["pending_phases"] = pending_phases(record.get("events", []))


def launch_instrumented(policy, service, journal, point, deadline, max_calls, records):
    command = [sys.executable, "-B", str(ROOT / "runtime_probe_worker.py"), policy,
               str(service), str(journal), "--crash", point, "--deadline", str(deadline),
               "--max-calls", str(max_calls)]
    try:
        result, record = observed_run(command, metadata={"policy": policy, "crash": point or None},
                                      timeout=TIMEOUT, capture_output=True, text=True)
    except subprocess.TimeoutExpired as error:
        records.append(error.runtime_record)
        raise
    records.append(record)
    return result


def quantiles(values):
    if not values:
        return {"count": 0}
    values = sorted(values)
    return {"count": len(values), "min_ms": values[0], "median_ms": values[(len(values)-1)//2],
            "p95_ms": values[math.ceil(.95*len(values))-1], "max_ms": values[-1]}


def timing_summary(records):
    phases = {}
    for record in records:
        for event in record.get("events", []):
            if "elapsed_ms" in event:
                phases.setdefault(event["phase"], []).append(event["elapsed_ms"])
    return {"launches": len(records), "timeouts": sum(r["timed_out"] for r in records),
            "wall_time": quantiles([r["duration_ms"] for r in records]),
            "child_cpu": quantiles([r["child_cpu_ms"] for r in records]),
            "phases": {name: quantiles(values) for name, values in sorted(phases.items())}}


def provenance():
    return {"python_version": sys.version.split()[0], "timeout_seconds": TIMEOUT,
            "source_sha256": {name: evaluation.digest(ROOT / name) for name in
                              ("runtime_audit.py", "runtime_probe_worker.py", "test_runtime_audit.py",
                               "RUNTIME_AUDIT.md", "comparison-v3.lock.json")},
            "model_calls": 0, "external_api_calls": 0}


def targeted(repeats=3):
    if not 1 <= repeats <= 10:
        raise ValueError("Choose between 1 and 10 repetitions")
    comparison3.verify()
    cases = {c["id"]: c for c in json.loads((ROOT / "comparison-v3.json").read_text())["cases"]}
    expected = json.loads((ROOT / "comparison-v3-results.json").read_text())["policies"]
    lookup = {policy: {r["id"]: r for r in rows} for policy, rows in expected.items()}
    episodes, records = [], []
    for repetition in range(repeats):
        for policy, case_id in TARGETS:
            for instrumented in (False, True):
                samples = []
                case = cases[case_id]
                if instrumented:
                    launcher = lambda *args: launch_instrumented(policy, *args, samples)
                    with patch.object(evaluation, "launch", launcher):
                        row = evaluation.run_case(case)
                else:
                    with observe_original(samples):
                        row = comparison3.run_case(case) if policy == "hybrid" else comparison.run_case(case, policy)
                for sample in samples:
                    sample.update(case_id=case_id, repetition=repetition, instrumented=instrumented)
                records.extend(samples)
                episodes.append({"case_id": case_id, "policy": policy, "repetition": repetition,
                                 "instrumented": instrumented, "matches_first_run": row == lookup[policy][case_id],
                                 "result": row})
        print(f"Completed targeted repetition {repetition + 1}/{repeats}", file=sys.stderr, flush=True)
    return {"scope": "Paired traced and original workers for previously timed-out cases",
            **provenance(), "episodes": episodes, "records": records, "summary": timing_summary(records)}


@contextmanager
def observe_original(records):
    original_run, original_case = subprocess.run, evaluation.run_case
    context = {}

    def case_wrapper(case):
        context["case_id"] = case["id"]
        return original_case(case)

    def run_wrapper(command, **kwargs):
        if kwargs.get("timeout") != TIMEOUT:
            raise ValueError("Original worker timeout changed")
        metadata = {**context, **describe_command(command), "instrumented": False}
        try:
            result, record = observed_run(command, original_run, metadata=metadata, **kwargs)
        except subprocess.TimeoutExpired as error:
            records.append(error.runtime_record)
            raise
        records.append(record)
        return result

    with patch.object(evaluation, "run_case", case_wrapper), patch.object(subprocess, "run", run_wrapper):
        yield


def write_new(path, content):
    with Path(path).open("x") as file:
        file.write(json.dumps(content, indent=2, sort_keys=True) + "\n")


def full(result_path, timing_path):
    # Outputs must be distinct and new: never erase an earlier failed attempt.
    if Path(result_path).resolve() == Path(timing_path).resolve():
        raise ValueError("Result and timing paths must differ")
    if Path(result_path).exists() or Path(timing_path).exists():
        raise FileExistsError("Audit outputs already exist")
    records = []
    with observe_original(records):
        result = comparison3.run()
    first = json.loads((ROOT / "comparison-v3-results.json").read_text())
    timing = {"scope": "Parent-only timings; original frozen worker commands and 15-second limits",
              **provenance(), "summary": timing_summary(records), "records": records,
              "matches_first_successful_report": result == first,
              "no_regression_goal_met": result["gates"]["no_regression_goal_met"]}
    write_new(result_path, result)
    write_new(timing_path, timing)
    return timing


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("targeted", "full"))
    parser.add_argument("--output", required=True)
    parser.add_argument("--result")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.mode == "targeted":
        if Path(args.output).exists():
            raise FileExistsError("Audit output already exists")
        report = targeted(args.repeats)
        write_new(args.output, report)
        passed = not report["summary"]["timeouts"] and all(r["matches_first_run"] for r in report["episodes"])
    else:
        if not args.result:
            parser.error("full requires --result")
        report = full(args.result, args.output)
        passed = report["matches_first_successful_report"] and report["no_regression_goal_met"]
    print(json.dumps({"passed": passed, "summary": report["summary"]}, indent=2))
    if not passed:
        raise SystemExit(1)
