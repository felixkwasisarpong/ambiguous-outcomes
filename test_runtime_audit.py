import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from runtime_audit import (observed_run, parse_events, pending_phases,
                           quantiles, targeted, timing_summary, write_new)


class RuntimeAuditTests(unittest.TestCase):
    def test_timeout_retains_partial_stage_and_original_exception(self):
        event = {"phase": "Journal.reserve", "event": "start", "monotonic_ns": 10, "cpu_ns": 20}
        stderr = ("RUNTIME_EVENT " + json.dumps(event) + "\n").encode()

        def fail(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"], stderr=stderr)

        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            observed_run(["synthetic-worker"], fail, timeout=15)
        record = caught.exception.runtime_record
        self.assertTrue(record["timed_out"])
        self.assertEqual(record["timeout_seconds"], 15)
        self.assertEqual(record["pending_phases"], ["Journal.reserve"])
        self.assertGreaterEqual(record["duration_ms"], 0)

    def test_cpu_duration_is_not_overwritten_by_absolute_cpu_clock(self):
        event = {"phase": "tool", "event": "end", "monotonic_ns": 2_000_000,
                 "cpu_ns": 5_000_000, "cpu_ms": 1, "elapsed_ms": 2}
        parsed = parse_events("RUNTIME_EVENT " + json.dumps(event), 1_000_000)[0]
        self.assertEqual(parsed["cpu_ms"], 1)
        self.assertEqual(parsed["process_cpu_ms"], 5)
        self.assertEqual(parsed["since_launch_ms"], 1)
        self.assertNotIn("monotonic_ns", parsed)

    def test_truncated_trace_does_not_mask_worker_failure(self):
        events = parse_events('RUNTIME_EVENT {"phase":', 0)
        self.assertEqual(events, [{"phase": "trace", "event": "truncated"}])

    def test_nested_phase_tracking_distinguishes_completed_calls(self):
        events = [{"phase": "worker", "event": "start"},
                  {"phase": "reserve", "event": "start"},
                  {"phase": "reserve", "event": "end"},
                  {"phase": "create", "event": "start"}]
        self.assertEqual(pending_phases(events), ["worker", "create"])

    def test_expected_crash_is_not_a_timeout(self):
        def crash(command, **kwargs):
            return subprocess.CompletedProcess(command, 73, stdout="", stderr="")
        result, record = observed_run(["synthetic-worker"], crash, timeout=15)
        self.assertEqual(result.returncode, 73)
        self.assertFalse(record["timed_out"])

    def test_run_wrapper_preserves_worker_output(self):
        result, record = observed_run([sys.executable, "-B", "-c", "print('synthetic')"],
                                      capture_output=True, text=True, timeout=15)
        self.assertEqual(result.stdout.strip(), "synthetic")
        self.assertEqual(record["returncode"], 0)
        self.assertGreater(record["duration_ms"], 0)

    def test_quantiles_include_slow_tail_and_do_not_drop_timeouts(self):
        stats = quantiles(list(range(1, 21)))
        self.assertEqual(stats["p95_ms"], 19)
        self.assertEqual(stats["max_ms"], 20)
        summary = timing_summary([{"timed_out": True, "duration_ms": 15001, "child_cpu_ms": 50, "events": []}])
        self.assertEqual(summary["timeouts"], 1)
        self.assertEqual(summary["wall_time"]["max_ms"], 15001)

    def test_outputs_cannot_overwrite_failed_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            write_new(path, {"accepted": False})
            with self.assertRaises(FileExistsError):
                write_new(path, {"accepted": True})
            self.assertFalse(json.loads(path.read_text())["accepted"])

    def test_zero_repeats_cannot_vacuously_pass(self):
        with self.assertRaises(ValueError):
            targeted(0)


if __name__ == "__main__":
    unittest.main()
