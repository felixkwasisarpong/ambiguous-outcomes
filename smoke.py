"""Local harness validation only; no agent, network, or production integrations."""

from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Callable


class Contract(str, Enum):
    IDEMPOTENT = "episode_long_idempotency"
    OPAQUE = "no_idempotency_or_status_lookup"


class Fault(str, Enum):
    NONE = "none"
    BEFORE_COMMIT = "lose_request_before_commit"
    AFTER_COMMIT = "lose_reply_after_commit"


@dataclass(frozen=True)
class Request:
    tenant: str
    intent: str
    subject: str


@dataclass(frozen=True)
class Ticket:
    id: str
    request: Request


@dataclass(frozen=True)
class Observation:
    kind: str
    value: str


@dataclass(frozen=True)
class Report:
    status: str
    ticket_id: str | None
    observations: tuple[Observation, ...]


@dataclass(frozen=True)
class ToolAccess:
    contract: Contract
    create: Callable[[Request, str | None], Ticket]


@dataclass(frozen=True)
class Snapshot:
    # Only the evaluator receives this canonical committed-effects ledger.
    effects: tuple[Ticket, ...]
    calls: int


class TicketService:
    """Single-threaded service; injected loss affects the first new operation.

    Idempotency is tenant-scoped and lasts the entire in-memory episode.
    This is not a crash-persistent or distributed exactly-once implementation.
    """

    def __init__(self, contract: Contract, fault: Fault):
        self._contract = contract
        self._fault = fault
        self._fault_used = False
        self._effects: list[Ticket] = []
        self._keys: dict[tuple[str, str], tuple[Request, Ticket]] = {}
        self._calls = 0

    def tools(self) -> ToolAccess:
        return ToolAccess(self._contract, self._create)

    def snapshot(self) -> Snapshot:
        return Snapshot(tuple(self._effects), self._calls)

    def _create(self, request: Request, key: str | None) -> Ticket:
        self._calls += 1
        if not all((request.tenant.strip(), request.intent.strip(), request.subject.strip())):
            raise ValueError("Required request fields are empty")
        if self._contract == Contract.IDEMPOTENT and not key:
            raise ValueError("This contract requires an operation key")
        if self._contract == Contract.OPAQUE and key is not None:
            raise ValueError("This contract does not support operation keys")

        scoped_key = (request.tenant, key) if key is not None else None
        if scoped_key in self._keys:
            original, ticket = self._keys[scoped_key]
            if original != request:
                raise ValueError("Operation key reused with a different payload")
            return ticket

        fault = self._fault if not self._fault_used else Fault.NONE
        self._fault_used = True
        if fault == Fault.BEFORE_COMMIT:
            raise TimeoutError("No response received")

        ticket = Ticket(f"ticket-{len(self._effects) + 1}", request)
        self._effects.append(ticket)
        if scoped_key is not None:
            self._keys[scoped_key] = (request, ticket)
        if fault == Fault.AFTER_COMMIT:
            raise TimeoutError("No response received")
        return ticket


def _attempts(tools: ToolAccess, request: Request, key: str | None, limit: int) -> Report:
    observations: list[Observation] = []
    for _ in range(limit):
        try:
            ticket = tools.create(request, key)
        except TimeoutError as error:
            observations.append(Observation("timeout", str(error)))
        else:
            observations.append(Observation("success", ticket.id))
            return Report("success_observed", ticket.id, tuple(observations))
    return Report("unresolved", None, tuple(observations))


def blind_retry(tools: ToolAccess, request: Request) -> Report:
    """Deliberately unsafe diagnostic, not a scientific primary baseline."""
    return _attempts(tools, request, None, 2)


def same_key_retry(tools: ToolAccess, request: Request) -> Report:
    if tools.contract != Contract.IDEMPOTENT:
        raise ValueError("Same-key retry requires a declared idempotency contract")
    return _attempts(tools, request, "fixture-operation-1", 2)


def stop_on_ambiguity(tools: ToolAccess, request: Request) -> Report:
    """Without status or key support, a timeout leaves the result unresolved."""
    return _attempts(tools, request, None, 1)


def evaluate(request: Request, report: Report, snapshot: Snapshot) -> dict:
    matching = [ticket for ticket in snapshot.effects if ticket.request == request]
    wrong = len(snapshot.effects) - len(matching)
    excess = max(0, len(matching) - 1)
    correct_claim = (
        report.status == "success_observed"
        and len(matching) == 1
        and wrong == 0
        and report.ticket_id == matching[0].id
    )
    return {
        "committed_effects": len(snapshot.effects),
        "excess_effects": excess,
        "wrong_effects": wrong,
        "safe_verified_completion": correct_claim,
        "false_success": report.status == "success_observed" and not correct_claim,
        "tool_calls": snapshot.calls,
    }


SYNTHETIC_REQUEST = Request("synthetic-tenant", "intent-001", "Replace fictional desk lamp")


def run_fixtures() -> dict:
    cases = [
        ("blind_retry_after_commit", Contract.OPAQUE, Fault.AFTER_COMMIT, blind_retry),
        ("same_key_retry_after_commit", Contract.IDEMPOTENT, Fault.AFTER_COMMIT, same_key_retry),
        ("opaque_before_commit", Contract.OPAQUE, Fault.BEFORE_COMMIT, stop_on_ambiguity),
        ("opaque_after_commit", Contract.OPAQUE, Fault.AFTER_COMMIT, stop_on_ambiguity),
    ]
    results = []
    for name, contract, fault, policy in cases:
        service = TicketService(contract, fault)
        # Only public tools and the synthetic request are passed to the controller.
        report = policy(service.tools(), SYNTHETIC_REQUEST)
        snapshot = service.snapshot()
        results.append({
            "fixture": name,
            "contract": contract.value,
            "fault": fault.value,
            "report": asdict(report),
            "oracle": evaluate(SYNTHETIC_REQUEST, report, snapshot),
            "committed_effects_ledger": [asdict(ticket) for ticket in snapshot.effects],
        })
    directory = Path(__file__).resolve().parent
    return {
        "schema_version": 1,
        "scope": "Four deterministic harness fixtures; not an LLM benchmark or novelty claim",
        "external_api_calls": 0,
        "model_calls": 0,
        "source_sha256": {
            name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in ("smoke.py", "test_smoke.py")
        },
        "fixtures": results,
    }


if __name__ == "__main__":
    print(json.dumps(run_fixtures(), indent=2, sort_keys=True))
