"""Local crash/restart simulator. Separate SQLite service and client journals.

Single controller, truthful episode-long contracts, no network or model calls.
Crash injection uses os._exit in a child process, never in the test runner.
"""

import argparse
from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from typing import Callable

from smoke import Observation, Report, Request, Snapshot, Ticket, evaluate, SYNTHETIC_REQUEST


CRASH_EXIT = 73
CRASH_POINTS = (
    "before_prepare", "after_prepare", "before_dispatch", "during_service_commit",
    "after_service_commit", "after_dispatch", "before_confirmation",
    "during_confirmation", "after_confirmation",
)


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA synchronous=FULL")
    return db


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Contract:
    idempotent: bool = True
    lookup: bool = True
    settlement_bound: int | None = 5


@dataclass(frozen=True)
class Fault:
    commit_delay: int = 0
    visibility_delay: int = 0
    drop_request: bool = False
    lose_reply: bool = True


@dataclass(frozen=True)
class Tools:
    contract: Contract
    now: Callable
    create: Callable
    find: Callable
    wait_until: Callable


class Service:
    """Durable virtual service; only tools() is passed to the controller.

    Idempotency records and committed effects share one SQLite transaction.
    Scheduled effects survive client/service process exits. First-create fault
    state and virtual time also persist. Keys never expire in this simulator.
    """

    def __init__(self, path, crash=lambda point: None):
        self.db = connect(path)
        self.crash = crash
        config = json.loads(self.db.execute("SELECT config FROM meta").fetchone()[0])
        self.contract = Contract(**config["contract"])
        self.fault = Fault(**config["fault"])
        self.tenant = config["tenant"]

    @classmethod
    def initialize(cls, path, contract=Contract(), fault=Fault()):
        if contract.settlement_bound is not None and contract.settlement_bound < 0:
            raise ValueError("Negative settlement bound")
        if min(fault.commit_delay, fault.visibility_delay) < 0:
            raise ValueError("Negative delay")
        if (not fault.drop_request and contract.settlement_bound is not None
                and fault.commit_delay + fault.visibility_delay > contract.settlement_bound):
            raise ValueError("Fault exceeds declared settlement bound")
        # Refuse to reset an existing service, even accidentally.
        with closing(connect(path)) as db:
            db.executescript("""
                CREATE TABLE meta(config TEXT NOT NULL, now INTEGER NOT NULL,
                    first_used INTEGER NOT NULL, calls INTEGER NOT NULL);
                CREATE TABLE actions(id INTEGER PRIMARY KEY, tenant TEXT NOT NULL,
                    op_key TEXT, payload TEXT NOT NULL, due INTEGER NOT NULL,
                    visible INTEGER NOT NULL, committed INTEGER NOT NULL,
                    UNIQUE(tenant, op_key));
            """)
            db.execute("INSERT INTO meta VALUES (?, 0, 0, 0)", (encode({
                "contract": asdict(contract), "fault": asdict(fault),
                "tenant": SYNTHETIC_REQUEST.tenant,
            }),))
            db.commit()

    def close(self):
        self.db.close()

    def now(self):
        return self.db.execute("SELECT now FROM meta").fetchone()[0]

    def tools(self):
        return Tools(self.contract, self.now, self.create, self.find, self.wait_until)

    def _validate(self, request):
        if request.tenant != self.tenant:
            raise PermissionError("Tenant not authorized")
        if not all((request.tenant.strip(), request.intent.strip(), request.subject.strip())):
            raise ValueError("Empty request")

    def _ticket(self, row):
        return Ticket(f"ticket-{row['id']}", Request(**json.loads(row["payload"])))

    def create(self, request, key):
        self._validate(request)
        if bool(key) != self.contract.idempotent:
            raise ValueError("Key does not match declared contract")
        payload = encode(asdict(request))
        with self.db:
            self.db.execute("UPDATE meta SET calls=calls+1")
            existing = self.db.execute(
                "SELECT * FROM actions WHERE tenant=? AND op_key=?", (request.tenant, key)
            ).fetchone() if key else None
            if existing:
                if existing["payload"] != payload:
                    raise ValueError("Operation key payload mismatch")
                pending = not existing["committed"]
                row = existing
                lost = False
            else:
                first = not self.db.execute("SELECT first_used FROM meta").fetchone()[0]
                self.db.execute("UPDATE meta SET first_used=1")
                fault = self.fault if first else Fault(lose_reply=False)
                lost = fault.lose_reply or fault.drop_request
                pending = fault.drop_request or fault.commit_delay > 0
                row = None
                if not fault.drop_request:
                    due = self.now() + fault.commit_delay
                    cursor = self.db.execute(
                        "INSERT INTO actions(tenant,op_key,payload,due,visible,committed) VALUES(?,?,?,?,?,?)",
                        (request.tenant, key, payload, due, due + fault.visibility_delay, int(not pending)),
                    )
                    row = self.db.execute("SELECT * FROM actions WHERE id=?", (cursor.lastrowid,)).fetchone()
            if row is not None and row["committed"]:
                self.crash("during_service_commit")
        # The commit above includes the effect AND its key. Die before returning.
        if row is not None and row["committed"]:
            self.crash("after_service_commit")
        if pending or lost:
            raise TimeoutError("No response received")
        return self._ticket(row)

    def find(self, request):
        self._validate(request)
        if not self.contract.lookup:
            raise ValueError("Lookup unsupported")
        with self.db:
            self.db.execute("UPDATE meta SET calls=calls+1")
        rows = self.db.execute(
            "SELECT * FROM actions WHERE payload=? AND committed=1 AND visible<=? ORDER BY id",
            (encode(asdict(request)), self.now()),
        )
        return tuple(self._ticket(row) for row in rows)

    def _advance(self, until):
        if until < self.now():
            raise ValueError("Clock cannot move backwards")
        self.db.execute("UPDATE meta SET now=?", (until,))
        self.db.execute("UPDATE actions SET committed=1 WHERE due<=?", (until,))

    def wait_until(self, until):
        with self.db:
            self.db.execute("UPDATE meta SET calls=calls+1")
            self._advance(until)

    def drain(self):
        """Evaluator only: observe effects arriving after the controller stops."""
        due = self.db.execute("SELECT MAX(due) FROM actions").fetchone()[0]
        with self.db:
            self._advance(max(self.now(), due or 0))

    def snapshot(self):
        rows = self.db.execute("SELECT * FROM actions WHERE committed=1 ORDER BY id")
        return Snapshot(tuple(self._ticket(row) for row in rows),
                        self.db.execute("SELECT calls FROM meta").fetchone()[0])


class Exhausted(Exception):
    pass


class Journal:
    """One immutable request/contract/budget/deadline per operation identity.

    Reservations are committed before tools run. A crash may waste a reserved
    call but cannot replenish the budget. Single controller per journal assumed.
    """

    def __init__(self, path, crash=lambda point: None):
        self.db = connect(path)
        self.crash = crash
        self.db.execute("""CREATE TABLE IF NOT EXISTS operations(
            operation TEXT PRIMARY KEY, payload TEXT NOT NULL, contract TEXT NOT NULL,
            op_key TEXT NOT NULL, deadline INTEGER NOT NULL, max_calls INTEGER NOT NULL,
            used INTEGER NOT NULL DEFAULT 0, submitted_at INTEGER, ticket TEXT)""")

    def close(self):
        self.db.close()

    def prepare(self, operation, request, contract, deadline, max_calls):
        if not operation or max_calls < 0 or deadline < 0:
            raise ValueError("Invalid operation limits")
        payload, policy = encode(asdict(request)), encode(asdict(contract))
        key = hashlib.sha256(encode([request.tenant, operation]).encode()).hexdigest()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO operations(operation,payload,contract,op_key,deadline,max_calls) VALUES(?,?,?,?,?,?)",
                            (operation, payload, policy, key, deadline, max_calls))
        row = self.get(operation)
        if (row["payload"], row["contract"], row["deadline"], row["max_calls"]) != (payload, policy, deadline, max_calls):
            raise ValueError("Persisted operation configuration mismatch")
        return row

    def get(self, operation):
        return self.db.execute("SELECT * FROM operations WHERE operation=?", (operation,)).fetchone()

    def reserve(self, operation, now, dispatch=False):
        with self.db:
            cursor = self.db.execute(
                "UPDATE operations SET used=used+1 WHERE operation=? AND used<max_calls AND ?>=0 AND ?<deadline",
                (operation, now, now),
            )
            if not cursor.rowcount:
                raise Exhausted("Persistent budget or deadline exhausted")
            if dispatch:
                self.db.execute("UPDATE operations SET submitted_at=? WHERE operation=?", (now, operation))

    def confirm(self, operation, ticket):
        with self.db:
            self.db.execute("UPDATE operations SET ticket=? WHERE operation=?", (ticket.id, operation))
            self.crash("during_confirmation")


def recover(journal, tools, request, operation="synthetic-operation", deadline=12,
            max_calls=12, crash=lambda point: None):
    observations = []

    def report(status, ticket_id=None):
        return Report(status, ticket_id, tuple(observations))

    def confirm(ticket):
        if ticket.request != request:
            return report("rejected")
        crash("before_confirmation")
        journal.confirm(operation, ticket)
        crash("after_confirmation")
        return report("success_observed", ticket.id)

    def dispatch():
        journal.reserve(operation, tools.now(), dispatch=True)
        crash("before_dispatch")
        try:
            ticket = tools.create(request, row["op_key"] if tools.contract.idempotent else None)
        except TimeoutError:
            observations.append(Observation("timeout", "No response received"))
            ticket = None
        crash("after_dispatch")
        return ticket

    try:
        crash("before_prepare")
        row = journal.prepare(operation, request, tools.contract, deadline, max_calls)
        crash("after_prepare")
        if row["ticket"]:
            observations.append(Observation("journal", "Previously confirmed result"))
            return report("success_observed", row["ticket"])
        if row["submitted_at"] is None:
            ticket = dispatch()
            if ticket:
                return confirm(ticket)

        while True:
            row = journal.get(operation)
            if tools.contract.lookup:
                journal.reserve(operation, tools.now())
                found = tools.find(request)
                observations.append(Observation("lookup", ",".join(t.id for t in found) or "missing"))
                if len(found) == 1:
                    return confirm(found[0])
                if len(found) > 1:
                    return report("duplicate_detected")

            bound = tools.contract.settlement_bound
            if bound is not None and tools.now() < row["submitted_at"] + bound:
                journal.reserve(operation, tools.now())
                tools.wait_until(min(row["submitted_at"] + bound, deadline))
                observations.append(Observation("wait", str(tools.now())))
                continue
            # Opaque absence is usable only after a bounded exact lookup.
            if not tools.contract.idempotent and (not tools.contract.lookup or bound is None):
                return report("unresolved")
            ticket = dispatch()
            if ticket:
                return confirm(ticket)
    except Exhausted:
        return report("budget_or_deadline_exhausted")
    except (PermissionError, ValueError):
        return report("rejected")


def worker(service_path, journal_path, crash_point="", deadline=12, max_calls=12):
    def crash(point):
        if point == crash_point:
            os._exit(CRASH_EXIT)
    service, journal = Service(service_path, crash), Journal(journal_path, crash)
    try:
        result = recover(journal, service.tools(), SYNTHETIC_REQUEST,
                         deadline=deadline, max_calls=max_calls, crash=crash)
        return asdict(result)
    finally:
        service.close()
        journal.close()


def launch(service_path, journal_path, crash_point="", deadline=12, max_calls=12):
    return subprocess.run([
        sys.executable, "-B", str(Path(__file__).resolve()), "--worker",
        str(service_path), str(journal_path), "--crash", crash_point,
        "--deadline", str(deadline), "--max-calls", str(max_calls),
    ], capture_output=True, text=True, timeout=15)


def run_fixtures():
    scenarios = [
        (point, Contract(), Fault(lose_reply=False), point) for point in CRASH_POINTS
    ] + [
        ("restart_pending_idempotent", Contract(), Fault(3, 2), "after_dispatch"),
        ("restart_pending_opaque", Contract(False, True, 5), Fault(3, 2), "after_dispatch"),
        ("restart_dropped_opaque", Contract(False, True, 5), Fault(drop_request=True), "after_dispatch"),
        ("restart_unknown_opaque", Contract(False, True, None), Fault(3, 2), "after_dispatch"),
        ("restart_key_without_lookup", Contract(True, False, None), Fault(), "after_service_commit"),
    ]
    output = []
    for name, contract, fault, point in scenarios:
        with tempfile.TemporaryDirectory() as directory:
            service_path, journal_path = Path(directory) / "service.db", Path(directory) / "journal.db"
            Service.initialize(service_path, contract, fault)
            interrupted = launch(service_path, journal_path, point)
            if interrupted.returncode != CRASH_EXIT:
                raise RuntimeError(f"Crash point not reached: {name}: {interrupted.stderr}")
            resumed = launch(service_path, journal_path)
            if resumed.returncode:
                raise RuntimeError(resumed.stderr)
            raw = json.loads(resumed.stdout)
            report = Report(raw["status"], raw["ticket_id"], tuple(Observation(**o) for o in raw["observations"]))
            service, journal = Service(service_path), Journal(journal_path)
            try:
                at_stop = evaluate(SYNTHETIC_REQUEST, report, service.snapshot())
                service.drain()
                snapshot = service.snapshot()
                row = journal.get("synthetic-operation")
                output.append({
                    "fixture": name, "crash_exit": interrupted.returncode,
                    "contract": asdict(contract), "fault": asdict(fault),
                    "report": raw, "at_stop": at_stop,
                    "eventual": evaluate(SYNTHETIC_REQUEST, report, snapshot),
                    "reserved_calls": row["used"],
                    "committed_effects_ledger": [asdict(t) for t in snapshot.effects],
                })
            finally:
                service.close()
                journal.close()
    directory = Path(__file__).resolve().parent
    return {
        "schema_version": 1,
        "scope": "Deterministic process-exit recovery fixtures; not power-loss or distributed validation",
        "external_api_calls": 0, "model_calls": 0,
        "source_sha256": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                          for name in ("smoke.py", "durable.py", "test_durable.py")},
        "fixtures": output,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", nargs=2)
    parser.add_argument("--crash", default="", choices=("",) + CRASH_POINTS)
    parser.add_argument("--deadline", type=int, default=12)
    parser.add_argument("--max-calls", type=int, default=12)
    args = parser.parse_args()
    result = worker(*args.worker, args.crash, args.deadline, args.max_calls) if args.worker else run_fixtures()
    print(json.dumps(result, indent=2, sort_keys=True))
