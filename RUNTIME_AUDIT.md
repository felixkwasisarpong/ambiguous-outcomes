# Runtime timeout audit

This audit adds diagnostics without editing any frozen strategy, case, scorer,
service, journal, virtual deadline, call budget or 15-second worker timeout.
It does not constitute a new recovery-policy evaluation version. All prior
successes and failures remain available.

## Questions and predeclared procedure

Measure interpreter/startup overhead, module import time, SQLite connection and
journal/service-operation durations. Keep wall and CPU time separate. An elapsed
time spike with low CPU does not by itself distinguish disk I/O, scheduling,
suspension or other host effects. Do not claim the historical root cause was
identified without a captured stall and supporting evidence.

Probe four historically affected cases: baseline `eval-v1-064`, and hybrid
`eval-v1-058`, `comparison-v3-065`, `comparison-v3-066`. For each, run three
repetitions with original workers and with traced workers, using fresh databases
and the same 15-second timeout. This produces 24 episodes. Both variants must
match the corresponding first successful report, including actual crash counts,
observations, ledger and resource accounting.

Traced workers emit minimal unbuffered events to stderr, so events survive abrupt
process exits. They record the earliest observable Python timestamp, imports and
method start/end events, including journal operations and public tools. They do
not log request payloads, database paths, environment variables or secrets. A
timeout retains the partial event stream and pending phases. A deliberately
injected exit with code 73 is recorded separately from a timeout. Diagnostic
logging has overhead; its timings are not claimed to be uninstrumented timings.

Then run the complete v3 comparison twice with original worker commands. A
parent-only observer measures subprocess wall/CPU duration, identifies the case
and policy, and preserves every timeout. It does not change the worker command,
source, timeout, crash schedule, resource limit or result. Full reports retain
v3's explicit reuse of 216 historical rows per previous policy. Timings are
separate, nondeterministic artifacts; compare behavioral JSON byte-for-byte with
the first successful v3 report. Do not merge targeted rechecks into a full result.

Use sequential runs without concurrent tests. An output path must be new; failed
attempts must not be overwritten. Two clean full reports matching the frozen
reference satisfy this audit's repeat check, not a guarantee against future host
stalls. If timeouts recur, retain the failures and investigate captured evidence;
do not quietly raise the timeout. Any changed limit requires a separate evaluation
version applied equally to all policies.

## Commands

```sh
python3 -B runtime_audit.py targeted --output runtime-targeted.json
python3 -B runtime_audit.py full --result runtime-full-1.json --output runtime-full-1-timings.json
python3 -B runtime_audit.py full --result runtime-full-2.json --output runtime-full-2-timings.json
```

Runtime reports include the Python version and source hashes. The instrumentation
uses POSIX child CPU accounting and is intended for macOS/Linux with Python 3.10+.
No external API calls, models, real customer data or paid services are used.
Synthetic timeout unit tests validate partial-event preservation without modifying
the timeout on any actual evaluation worker.
