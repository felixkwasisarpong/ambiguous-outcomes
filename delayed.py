"""Phase-one deterministic fixtures: delayed commits and stale reads, no LLMs."""

from dataclasses import asdict, dataclass
import hashlib
import heapq
import json
from pathlib import Path
from typing import Callable

from smoke import Request, Ticket, Report, Observation, Snapshot, evaluate, SYNTHETIC_REQUEST


class BudgetExceeded(Exception):
    pass


@dataclass(frozen=True)
class Contract:
    # Combined maximum time from submission to commit AND lookup visibility.
    # None means the controller is not promised a useful settlement bound.
    settlement_bound: int | None


@dataclass(frozen=True)
class FaultPlan:
    commit_delay: int = 0
    visibility_delay: int = 0
    drop_request: bool = False


@dataclass(frozen=True)
class Effect:
    ticket: Ticket
    committed_at: int
    visible_at: int


@dataclass(frozen=True)
class Tools:
    contract: Contract
    now: Callable[[], int]
    create: Callable[[Request], Ticket]
    find: Callable[[Request], tuple[Ticket, ...]]
    wait_until: Callable[[int], None]


class DelayedService:
    """Single writer, no key support, exact-request lookup, first reply lost.

    Faults attach to the first creation attempt; reads do not consume faults.
    The declared settlement bound is assumed truthful and checked at setup.
    Evaluator-only drain() observes late effects without exposing them to policy.
    """

    def __init__(self, contract: Contract, fault: FaultPlan, max_calls: int = 8,
                 allowed_tenant: str = "synthetic-tenant"):
        if min(fault.commit_delay, fault.visibility_delay, max_calls) < 0:
            raise ValueError("Delays and call budget must be nonnegative")
        if contract.settlement_bound is not None:
            if contract.settlement_bound < 0:
                raise ValueError("Settlement bound must be nonnegative")
            if not fault.drop_request and fault.commit_delay + fault.visibility_delay > contract.settlement_bound:
                raise ValueError("Fault exceeds declared settlement contract")
        self._contract, self._fault = contract, fault
        self._max_calls, self._allowed_tenant = max_calls, allowed_tenant
        self._now = self._calls = self._creates = self._sequence = 0
        self._pending: list[tuple[int, int, int, Request]] = []
        self._ledger: list[Effect] = []

    def tools(self) -> Tools:
        return Tools(self._contract, lambda: self._now, self._create, self._find, self._wait_until)

    def snapshot(self) -> Snapshot:
        return Snapshot(tuple(e.ticket for e in self._ledger), self._calls)

    def ledger(self) -> tuple[Effect, ...]:
        return tuple(self._ledger)

    def _spend(self):
        if self._calls >= self._max_calls:
            raise BudgetExceeded("Tool call budget exhausted")
        self._calls += 1

    def _advance(self, until: int):
        if until < self._now:
            raise ValueError("Virtual time cannot move backwards")
        while self._pending and self._pending[0][0] <= until:
            due, _, visible, request = heapq.heappop(self._pending)
            ticket = Ticket(f"ticket-{len(self._ledger) + 1}", request)
            self._ledger.append(Effect(ticket, due, visible))
        self._now = until

    def _validate(self, request: Request):
        if not all((request.tenant.strip(), request.intent.strip(), request.subject.strip())):
            raise ValueError("Required request fields are empty")
        if request.tenant != self._allowed_tenant:
            raise PermissionError("Tenant not authorized")

    def _create(self, request: Request) -> Ticket:
        self._spend()
        self._validate(request)
        first = self._creates == 0
        self._creates += 1
        if first and self._fault.drop_request:
            raise TimeoutError("No response received")
        commit_delay = self._fault.commit_delay if first else 0
        visibility_delay = self._fault.visibility_delay if first else 0
        due = self._now + commit_delay
        self._sequence += 1
        heapq.heappush(self._pending, (due, self._sequence, due + visibility_delay, request))
        self._advance(self._now)
        if first:
            raise TimeoutError("No response received")
        return self._ledger[-1].ticket

    def _find(self, request: Request) -> tuple[Ticket, ...]:
        self._spend()
        self._validate(request)
        return tuple(e.ticket for e in self._ledger if e.ticket.request == request and e.visible_at <= self._now)

    def _wait_until(self, until: int):
        self._spend()
        self._advance(until)

    def drain(self):
        """Evaluator only: finish pending commits even after a controller stops."""
        if self._pending:
            self._advance(max(event[0] for event in self._pending))


def recover(tools: Tools, request: Request, deadline: int = 10, *, unsafe_early_retry=False) -> Report:
    """Bound-aware reference component, NOT the complete durable B1 baseline.

    unsafe_early_retry is an explicitly labelled negative control. The safe path
    never treats an early negative lookup as permission to repeat an action.
    """
    observations: list[Observation] = []
    submitted_at = tools.now()

    def observe(kind, value):
        observations.append(Observation(kind, f"t={tools.now()}: {value}"))

    def finish(status, ticket_id=None):
        return Report(status, ticket_id, tuple(observations))

    if deadline < submitted_at:
        return finish("deadline_exhausted")
    try:
        try:
            ticket = tools.create(request)
        except TimeoutError as error:
            observe("timeout", str(error))
        else:
            observe("success", ticket.id)
            return finish("success_observed", ticket.id)

        found = tools.find(request)
        observe("lookup", ",".join(t.id for t in found) or "missing")
        if len(found) == 1:
            return finish("success_observed", found[0].id)
        if len(found) > 1:
            return finish("duplicate_detected")

        bound = tools.contract.settlement_bound
        if not unsafe_early_retry:
            if bound is None:
                return finish("unresolved")
            settled_at = submitted_at + bound
            tools.wait_until(min(settled_at, deadline))
            observe("wait", "finished")
            found = tools.find(request)
            observe("lookup", ",".join(t.id for t in found) or "missing")
            if len(found) == 1:
                return finish("success_observed", found[0].id)
            if len(found) > 1:
                return finish("duplicate_detected")
            if tools.now() < settled_at or tools.now() >= deadline:
                return finish("unresolved")

        ticket = tools.create(request)
        observe("success", ticket.id)
        return finish("success_observed", ticket.id)
    except BudgetExceeded as error:
        observe("budget", str(error))
        return finish("budget_exhausted")
    except (PermissionError, ValueError) as error:
        observe("rejected", str(error))
        return finish("rejected")


def run_fixtures() -> dict:
    scenarios = [
        ("early_negative_read_retry", Contract(5), FaultPlan(3, 2), 10, True),
        ("wait_for_delayed_commit", Contract(5), FaultPlan(3, 2), 10, False),
        ("wait_for_stale_read", Contract(5), FaultPlan(0, 4), 10, False),
        ("deadline_before_commit", Contract(5), FaultPlan(3, 2), 2, False),
        ("retry_after_settled_absence", Contract(5), FaultPlan(drop_request=True), 10, False),
        ("unknown_bound_abstains", Contract(None), FaultPlan(3, 2), 10, False),
    ]
    output = []
    for name, contract, fault, deadline, unsafe in scenarios:
        service = DelayedService(contract, fault)
        report = recover(service.tools(), SYNTHETIC_REQUEST, deadline, unsafe_early_retry=unsafe)
        stopped_at = service.tools().now()
        before_drain = evaluate(SYNTHETIC_REQUEST, report, service.snapshot())
        service.drain()
        output.append({
            "fixture": name, "negative_control": unsafe,
            "contract": asdict(contract), "fault": asdict(fault),
            "deadline": deadline, "controller_stopped_at": stopped_at,
            "report": asdict(report), "at_stop": before_drain,
            "eventual": evaluate(SYNTHETIC_REQUEST, report, service.snapshot()),
            "committed_effects_ledger": [asdict(e) for e in service.ledger()],
        })
    directory = Path(__file__).resolve().parent
    return {
        "schema_version": 1,
        "scope": "Deterministic delayed-commit harness validation; no model comparison",
        "model_calls": 0, "external_api_calls": 0,
        "source_sha256": {
            name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in ("smoke.py", "delayed.py", "test_delayed.py")
        },
        "fixtures": output,
    }


if __name__ == "__main__":
    print(json.dumps(run_fixtures(), indent=2, sort_keys=True))
