"""Diagnostic worker only: no changes to frozen controllers or limits."""
import time

BOOT_NS = time.monotonic_ns()
import json
import os
import sys


def event(phase, kind, **values):
    payload = {"phase": phase, "event": kind, "monotonic_ns": time.monotonic_ns(),
               "cpu_ns": time.process_time_ns(), **values}
    # Unbuffered events survive os._exit. Do not log arguments, paths or payloads.
    os.write(2, ("RUNTIME_EVENT " + json.dumps(payload, sort_keys=True) + "\n").encode())


def timed(phase, function):
    def wrapper(*args, **kwargs):
        start, cpu = time.monotonic_ns(), time.process_time_ns()
        event(phase, "start")
        try:
            return function(*args, **kwargs)
        finally:
            event(phase, "end", elapsed_ms=(time.monotonic_ns() - start) / 1e6,
                  cpu_ms=(time.process_time_ns() - cpu) / 1e6)
    return wrapper


def main():
    event("bootstrap", "ready", first_observable_ns=BOOT_NS)
    import_start = time.monotonic_ns()
    event("imports", "start")
    import argparse
    import comparison
    import comparison3
    import durable
    event("imports", "end", elapsed_ms=(time.monotonic_ns() - import_start) / 1e6)

    parser = argparse.ArgumentParser()
    parser.add_argument("policy", choices=("baseline", "key_first", "hybrid"))
    parser.add_argument("service")
    parser.add_argument("journal")
    parser.add_argument("--crash", default="")
    parser.add_argument("--deadline", type=int, required=True)
    parser.add_argument("--max-calls", type=int, required=True)
    args = parser.parse_args()
    for cls, names in [(durable.Service, ("__init__", "create", "find", "wait_until", "close")),
                       (durable.Journal, ("__init__", "prepare", "reserve", "confirm", "get", "close"))]:
        for name in names:
            setattr(cls, name, timed(f"{cls.__name__}.{name}", getattr(cls, name)))
    durable.connect = timed("sqlite_connect", durable.connect)
    if args.policy == "hybrid":
        run = lambda: comparison3.worker(args.service, args.journal, args.crash, args.deadline, args.max_calls)
    else:
        run = lambda: comparison.worker(args.policy, args.service, args.journal, args.crash, args.deadline, args.max_calls)
    result = timed("worker", run)()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
