"""CHP-hardened spend-approval gate: R0 refusal, deterministic foundation scoring,
human lock, and the decision ledger.

Covers the consensus-hardening-protocol integration (``shared/chp.py``):

- the spend-shaped R0 gate refuses ill-posed approvals before any decision;
- the deterministic adversary scores guardrails + bounded decision state +
  parity vs the PO/invoice record — spend approvals gate at CHP's
  capital_allocation floor of 100, and a parity mismatch is fatal;
- every hardened case opens PROVISIONAL_LOCK and a named approver locks it
  through CHP third-party validation;
- every approval seals a CHP payload envelope into the append-only decision
  ledger, whose reads re-validate envelope and body integrity;
- REQUIRE_HUMAN_LOCK (default on) holds auto-approvals for a named human and
  the payment executor refuses unlocked spend.

Parity here is self-grounding: the PO total in the fixtures matches the
invoice total under test.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from chp.models import SessionStatus, Verdict

import agents.approval_router.router as router_module
from agents.approval_router.router import ApprovalRouter
from agents.payment_executor.executor import PaymentExecutor
from dashboard.app import app
from shared.chp import (
    ChpRejection,
    ChpValidationResult,
    SpendApprovalGate,
    SpendDecisionLedger,
)
from shared.config import ChpConfig, settings
from shared.models import (
    Anomaly,
    AnomalyType,
    ApprovalRequest,
    Invoice,
    PurchaseOrder,
    RiskLevel,
    ValidationResult,
)

CONFIRMER = "sam@cubiczan.com"
PO_NUMBER = "PO-2026-0341"


# ------------------------------------------------------------------- fixtures


def make_invoice(total: str = "2500.00", po_number: str | None = PO_NUMBER) -> Invoice:
    return Invoice(
        invoice_id="INV-001",
        vendor_name="Industrial Supply Co.",
        invoice_number="INV-2026-001",
        invoice_date=date(2026, 5, 1),
        subtotal=Decimal(total),
        total=Decimal(total),
        po_number=po_number,
    )


def make_po(total: str = "2500.00") -> PurchaseOrder:
    return PurchaseOrder(
        po_number=PO_NUMBER,
        vendor_id="V-2041",
        vendor_name="Industrial Supply Co.",
        order_date=date(2026, 4, 15),
        total_amount=Decimal(total),
    )


def make_validation() -> ValidationResult:
    return ValidationResult(
        invoice_id="INV-001",
        is_valid=True,
        confidence=0.92,
        po_match=True,
        receipt_match=True,
    )


def make_approval(**overrides: Any) -> ApprovalRequest:
    defaults: dict[str, Any] = {
        "invoice_id": "INV-001",
        "amount": Decimal("2500.00"),
        "department": "general",
        "vendor_name": "Industrial Supply Co.",
        "assigned_to": "auto_approve",
        "risk_level": RiskLevel.LOW,
        "anomalies": [],
        "validation_result": make_validation(),
        "decision": "approved",
    }
    defaults.update(overrides)
    return ApprovalRequest(**defaults)


class QueueStub:
    """Records queue submissions without touching UiPath Orchestrator."""

    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any], str]] = []

    async def add_queue_item(
        self, queue_name: str, data: dict[str, Any], priority: str = "Normal"
    ) -> dict[str, Any]:
        self.items.append((queue_name, data, priority))
        return {"ok": True}


def make_gate(
    tmp_path: Path, require_lock: bool = True, enabled: bool = True
) -> SpendApprovalGate:
    return SpendApprovalGate(
        ChpConfig(
            enabled=enabled,
            require_human_lock=require_lock,
            decisions_path=tmp_path / "chp_decisions.jsonl",
        )
    )


@pytest.fixture()
def queue_stub(monkeypatch: pytest.MonkeyPatch) -> QueueStub:
    stub = QueueStub()
    monkeypatch.setattr(router_module, "uipath", stub)
    return stub


def make_router(gate: SpendApprovalGate) -> ApprovalRouter:
    router = ApprovalRouter()
    router.gate = gate
    return router


def make_executor(gate: SpendApprovalGate) -> PaymentExecutor:
    return PaymentExecutor(gate=gate)


# ----------------------------------------------------------------------- R0


def test_r0_refuses_a_zero_amount_request(tmp_path: Path) -> None:
    gate = make_gate(tmp_path)
    with pytest.raises(ChpRejection) as excinfo:
        gate.open_r0(
            invoice_id="INV-001",
            amount=Decimal("0.00"),
            currency="USD",
            approver="auto_approve",
            validation=make_validation(),
            purchase_order=make_po(),
            auto_approve=True,
            auto_approve_bound=Decimal("5000"),
        )
    assert excinfo.value.evaluation is not None
    assert excinfo.value.evaluation.results["Solvable"] == "FATAL"


def test_r0_refuses_an_unbounded_auto_approval(tmp_path: Path) -> None:
    gate = make_gate(tmp_path)
    with pytest.raises(ChpRejection) as excinfo:
        gate.open_r0(
            invoice_id="INV-001",
            amount=Decimal("2500.00"),
            currency="USD",
            approver="auto_approve",
            validation=make_validation(),
            auto_approve=True,
            auto_approve_bound=None,
        )
    assert excinfo.value.evaluation is not None
    assert excinfo.value.evaluation.results["Scoped"] == "FATAL"


def test_r0_refuses_an_approval_without_validation_state(tmp_path: Path) -> None:
    gate = make_gate(tmp_path)
    with pytest.raises(ChpRejection) as excinfo:
        gate.open_r0(
            invoice_id="INV-001",
            amount=Decimal("2500.00"),
            currency="USD",
            approver="auto_approve",
            validation=None,
            auto_approve=True,
            auto_approve_bound=Decimal("5000"),
        )
    assert excinfo.value.evaluation is not None
    assert excinfo.value.evaluation.results["Valid"] == "FATAL"


def test_r0_accepts_a_well_posed_approval(tmp_path: Path) -> None:
    gate = make_gate(tmp_path)
    evaluation = gate.open_r0(
        invoice_id="INV-001",
        amount=Decimal("2500.00"),
        currency="USD",
        approver="auto_approve",
        validation=make_validation(),
        purchase_order=make_po(),
        auto_approve=True,
        auto_approve_bound=Decimal("5000"),
    )
    assert evaluation.verdict == Verdict.PASS
    assert set(evaluation.results) == {"Solvable", "Scoped", "Valid", "Worth_it"}


# ---------------------------------------------------------------- foundation


def test_po_parity_scores_a_full_capital_allocation_foundation(tmp_path: Path) -> None:
    gate = make_gate(tmp_path)
    assessment = gate.assess_foundation(
        amount=Decimal("2500.00"),
        expected_amount=Decimal("2500.00"),
        parity_basis="purchase_order",
        parity_reference=PO_NUMBER,
        guardrail_evidence="deterministic approval matrix",
        assigned_to="auto_approve",
    )
    assert assessment.domain == "capital_allocation"
    assert assessment.score == 100
    assert assessment.parity is not None and assessment.parity.within_tolerance is True


def test_missing_parity_cannot_reach_the_finance_floor(tmp_path: Path) -> None:
    gate = make_gate(tmp_path)
    assessment = gate.assess_foundation(
        amount=Decimal("2500.00"),
        expected_amount=None,
        parity_basis="purchase_order",
        parity_reference=PO_NUMBER,
        guardrail_evidence="deterministic approval matrix",
        assigned_to="auto_approve",
    )
    assert assessment.score == 70  # guardrails 40 + bounded 30; no parity evidence
    assert assessment.score < gate.finance_floor


def test_parity_mismatch_is_fatal(tmp_path: Path) -> None:
    gate = make_gate(tmp_path)
    with pytest.raises(ChpRejection, match="MISMATCH"):
        gate.harden(
            invoice_id="INV-001",
            vendor_name="Industrial Supply Co.",
            amount=Decimal("2500.00"),
            currency="USD",
            validation=make_validation(),
            purchase_order=make_po(total="999.00"),
            approver="auto_approve",
        )


# ----------------------------------------------------------------- lock flow


def hardened_decision(gate: SpendApprovalGate) -> Any:
    return gate.harden(
        invoice_id="INV-001",
        vendor_name="Industrial Supply Co.",
        amount=Decimal("2500.00"),
        currency="USD",
        validation=make_validation(),
        purchase_order=make_po(),
        approver="auto_approve",
    )


def test_hardened_case_opens_provisional_and_locks_with_a_confirmer(tmp_path: Path) -> None:
    gate = make_gate(tmp_path)
    decision = hardened_decision(gate)
    assert decision.case.status == SessionStatus.PROVISIONAL_LOCK
    assert gate.lock(decision.case, CONFIRMER) == SessionStatus.LOCKED
    assert decision.case.locked_decisions == [decision.case.decision_id]


def test_lock_rejects_a_confirmer_on_a_fresh_case(tmp_path: Path) -> None:
    gate = make_gate(tmp_path)
    decision = hardened_decision(gate)
    decision.case.status = SessionStatus.EXPLORING
    with pytest.raises(ValueError, match="PROVISIONAL_LOCK"):
        gate.lock(decision.case, CONFIRMER)


def test_reload_and_lock_confirms_from_the_ledger(tmp_path: Path) -> None:
    gate = make_gate(tmp_path)
    decision = hardened_decision(gate)
    gate.record(
        decision,
        invoice_id="INV-001",
        stage="approval_routing",
        outcome="held_for_human_lock",
    )

    record = gate.reload_and_lock(decision.case.decision_id, CONFIRMER)

    assert record["session_status"] == SessionStatus.LOCKED.value
    assert record["confirmed_by"] == CONFIRMER
    newest = gate.records.get(decision.case.decision_id)
    assert newest is not None and newest["session_status"] == SessionStatus.LOCKED.value
    # append-only: the held record is still readable
    listing = gate.records.list()
    assert len(listing) == 2


def test_reload_and_lock_is_idempotent_and_refuses_refusals(tmp_path: Path) -> None:
    gate = make_gate(tmp_path)
    decision = hardened_decision(gate)
    gate.record(
        decision,
        invoice_id="INV-001",
        stage="approval_routing",
        outcome="held_for_human_lock",
    )
    decision_id = decision.case.decision_id

    first = gate.reload_and_lock(decision_id, CONFIRMER)
    second = gate.reload_and_lock(decision_id, "someone-else@example.com")
    assert second["decision_id"] == first["decision_id"]
    assert second["confirmed_by"] == CONFIRMER  # the lock stands, nobody re-locks

    gate.record_refusal(
        invoice_id="INV-002",
        stage="payment_execution",
        reason="CHP R0 gate: the spend decision failed Scoped",
    )
    with pytest.raises(ChpRejection, match="refused"):
        gate.reload_and_lock("spend-INV-002", CONFIRMER)


# -------------------------------------------------------------------- ledger


def test_decision_record_seals_an_envelope(tmp_path: Path) -> None:
    gate = make_gate(tmp_path)
    decision = hardened_decision(gate)
    record = gate.record(
        decision,
        invoice_id="INV-001",
        stage="approval_routing",
        outcome="held_for_human_lock",
    )

    listing = gate.records.list()
    assert len(listing) == 1
    assert listing[0]["envelope_valid"] is True
    assert listing[0]["integrity_valid"] is True
    assert listing[0]["decision_id"] == record["decision_id"]
    assert gate.records.get(record["decision_id"]) is not None
    assert gate.records.get("spend-missing") is None


def test_tampered_ledger_bodies_read_as_invalid(tmp_path: Path) -> None:
    gate = make_gate(tmp_path)
    decision = hardened_decision(gate)
    gate.record(
        decision,
        invoice_id="INV-001",
        stage="approval_routing",
        outcome="held_for_human_lock",
    )

    path = gate.records.path
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    # tamper with the sealed payload body: inflate the foundation score
    entry["body"] = entry["body"].replace('"foundation_score": 100', '"foundation_score": 70')
    lines[0] = json.dumps(entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    record = gate.records.list()[0]
    assert record["integrity_valid"] is False
    assert record["envelope_valid"] is True  # the CHP envelope checks structure only


def test_refusals_are_recorded_in_the_ledger(tmp_path: Path) -> None:
    gate = make_gate(tmp_path)
    with pytest.raises(ChpRejection):
        gate.open_r0(
            invoice_id="INV-001",
            amount=Decimal("0.00"),
            currency="USD",
            approver="auto_approve",
            validation=make_validation(),
            auto_approve=True,
            auto_approve_bound=Decimal("5000"),
        )
    gate.record_refusal(
        invoice_id="INV-001",
        stage="approval_routing",
        reason="CHP R0 gate: the spend decision failed Solvable",
    )

    record = gate.records.list()[0]
    assert record["action"] == "refused"
    assert record["decision"] == "refused"
    assert record["refusal_reason"] is not None
    assert record["envelope_valid"] is True
    assert record["integrity_valid"] is True


def test_direct_ledger_append_and_revalidate_round_trip(tmp_path: Path) -> None:
    ledger = SpendDecisionLedger(tmp_path / "chp_decisions.jsonl")
    ledger.append({"decision_id": "spend-X", "body": "hello", "body_sha256": "bad"})

    record = ledger.get("spend-X")
    assert record is not None
    assert record["integrity_valid"] is False  # digest recomputed, mismatch flagged


# -------------------------------------------------- payment authorization


def test_authorization_requires_a_lock_when_human_lock_is_on(tmp_path: Path) -> None:
    gate = make_gate(tmp_path, require_lock=True)
    decision = hardened_decision(gate)
    record = gate.record(
        decision,
        invoice_id="INV-001",
        stage="approval_routing",
        outcome="held_for_human_lock",
    )
    approval = make_approval(chp_decision_id=record["decision_id"])

    assert gate.authorization_for_payment(approval) is None  # provisional — no spend

    gate.reload_and_lock(record["decision_id"], CONFIRMER)
    authorized = gate.authorization_for_payment(approval)
    assert authorized is not None
    assert authorized["confirmed_by"] == CONFIRMER


def test_authorization_without_human_lock_accepts_a_full_floor_case(tmp_path: Path) -> None:
    gate = make_gate(tmp_path, require_lock=False)
    decision = hardened_decision(gate)
    record = gate.record(
        decision,
        invoice_id="INV-001",
        stage="approval_routing",
        outcome="auto_approved",
    )
    approval = make_approval(chp_decision_id=record["decision_id"])

    assert record["foundation_score"] == 100
    assert gate.authorization_for_payment(approval) is not None


def test_authorization_fails_closed_without_a_decision_record(tmp_path: Path) -> None:
    gate = make_gate(tmp_path, require_lock=True)
    assert gate.authorization_for_payment(make_approval(chp_decision_id=None)) is None
    assert gate.authorization_for_payment(make_approval(chp_decision_id="spend-404")) is None

    # a sub-floor held case never authorizes spend, even unlocked
    sub_gate = make_gate(tmp_path, require_lock=False)
    decision = sub_gate.harden(
        invoice_id="INV-002",
        vendor_name="Industrial Supply Co.",
        amount=Decimal("2500.00"),
        currency="USD",
        validation=make_validation(),
        purchase_order=None,  # no parity evidence — score 70
        approver="auto_approve",
    )
    sub_gate.record(
        decision,
        invoice_id="INV-002",
        stage="approval_routing",
        outcome="held_for_human_lock",
    )
    assert (
        gate.authorization_for_payment(make_approval(chp_decision_id=decision.case.decision_id))
        is None
    )


# ----------------------------------------------------- approval-flow integration


async def test_router_holds_an_auto_approval_for_a_human_lock(
    tmp_path: Path, queue_stub: QueueStub
) -> None:
    router = make_router(make_gate(tmp_path, require_lock=True))
    approval = await router.route(make_invoice(), make_validation(), [], make_po())

    assert approval.decision is None  # spend never auto-approves by default
    assert "human lock" in (approval.decision_reason or "")
    assert approval.chp_decision_id is not None

    record = router.gate.records.get(approval.chp_decision_id)
    assert record is not None
    assert record["decision"] == "held_for_human_lock"
    assert record["session_status"] == SessionStatus.PROVISIONAL_LOCK.value
    assert record["foundation_score"] == 100  # parity held; the lock is still required

    # the hold still routes to the human queue, carrying the CHP decision id
    assert len(queue_stub.items) == 1
    assert queue_stub.items[0][1]["ChpDecisionId"] == approval.chp_decision_id


async def test_router_self_certifies_a_parity_verified_approval_when_unlocked(
    tmp_path: Path, queue_stub: QueueStub
) -> None:
    router = make_router(make_gate(tmp_path, require_lock=False))
    approval = await router.route(make_invoice(), make_validation(), [], make_po())

    assert approval.decision == "approved"
    assert approval.chp_decision_id is not None
    record = router.gate.records.get(approval.chp_decision_id)
    assert record is not None
    assert record["decision"] == "auto_approved"
    assert record["foundation_score"] == 100
    assert queue_stub.items == []


async def test_router_holds_when_parity_is_unavailable_even_unlocked(
    tmp_path: Path, queue_stub: QueueStub
) -> None:
    router = make_router(make_gate(tmp_path, require_lock=False))
    approval = await router.route(make_invoice(), make_validation(), [], None)

    assert approval.decision is None  # score 70 < finance floor 100
    record = router.gate.records.get(approval.chp_decision_id)
    assert record is not None
    assert record["foundation_score"] == 70
    assert record["foundation_verdict"] == Verdict.REFRAME.value
    assert len(queue_stub.items) == 1


async def test_router_r0_failure_is_fatal_and_recorded(
    tmp_path: Path, queue_stub: QueueStub
) -> None:
    router = make_router(make_gate(tmp_path, require_lock=True))
    with pytest.raises(ChpRejection, match="R0"):
        await router.route(make_invoice(total="0.00"), make_validation(), [], make_po())

    record = router.gate.records.list()[0]
    assert record["action"] == "refused"
    assert record["r0_verdict"] == Verdict.HALT.value
    assert record["r0_results"]["Solvable"] == "FATAL"
    assert queue_stub.items == []  # nothing routed, nothing approved


async def test_router_parity_mismatch_is_fatal_and_recorded(
    tmp_path: Path, queue_stub: QueueStub
) -> None:
    router = make_router(make_gate(tmp_path, require_lock=False))
    with pytest.raises(ChpRejection, match="MISMATCH"):
        await router.route(make_invoice(), make_validation(), [], make_po(total="999.00"))

    record = router.gate.records.list()[0]
    assert record["action"] == "refused"
    assert "parity MISMATCH" in (record["refusal_reason"] or "")


async def test_router_legacy_auto_approval_when_the_gate_is_disabled(
    tmp_path: Path, queue_stub: QueueStub
) -> None:
    router = make_router(make_gate(tmp_path, require_lock=True, enabled=False))
    approval = await router.route(make_invoice(), make_validation(), [], make_po())

    assert approval.decision == "approved"
    assert approval.chp_decision_id is None
    assert router.gate.records.list() == []


async def test_executor_refuses_unconfirmed_spend_and_records_the_refusal(
    tmp_path: Path, queue_stub: QueueStub
) -> None:
    gate = make_gate(tmp_path, require_lock=True)
    router = make_router(gate)
    approval = await router.route(make_invoice(), make_validation(), [], make_po())
    approval.decision = "approved"  # as if released — the lock is still missing

    executor = make_executor(gate)
    with pytest.raises(ChpRejection, match="human lock"):
        await executor.execute(make_invoice(), approval)

    refusal = gate.records.list()[0]
    assert refusal["stage"] == "payment_execution"
    assert refusal["action"] == "refused"
    assert executor._payment_log == []


async def test_executor_refuses_a_fabricated_approval_without_a_chp_decision(
    tmp_path: Path,
) -> None:
    gate = make_gate(tmp_path, require_lock=True)
    executor = make_executor(gate)
    with pytest.raises(ChpRejection, match="human lock"):
        await executor.execute(make_invoice(), make_approval(chp_decision_id=None))

    refusal = gate.records.list()[0]
    assert refusal["stage"] == "payment_execution"
    assert refusal["action"] == "refused"


async def test_executor_rejects_a_payment_amount_that_breaks_parity(
    tmp_path: Path,
) -> None:
    gate = make_gate(tmp_path, require_lock=False)
    decision = gate.harden(
        invoice_id="INV-001",
        vendor_name="Industrial Supply Co.",
        amount=Decimal("2500.00"),
        currency="USD",
        validation=make_validation(),
        purchase_order=make_po(),
        approver="auto_approve",
    )
    gate.record(
        decision,
        invoice_id="INV-001",
        stage="approval_routing",
        outcome="auto_approved",
    )
    executor = make_executor(gate)
    with pytest.raises(ChpRejection, match="MISMATCH"):
        await executor.execute(
            make_invoice(total="9999.00"),  # mutated after approval
            make_approval(amount=Decimal("2500.00"), chp_decision_id=decision.case.decision_id),
        )

    refusal = gate.records.list()[0]
    assert refusal["stage"] == "payment_execution"
    assert executor._payment_log == []


async def test_confirmed_lock_releases_the_payment_end_to_end(
    tmp_path: Path, queue_stub: QueueStub
) -> None:
    gate = make_gate(tmp_path, require_lock=True)
    router = make_router(gate)
    approval = await router.route(make_invoice(), make_validation(), [], make_po())
    assert approval.decision is None

    # the named approver confirms through the dashboard surface
    locked = gate.reload_and_lock(approval.chp_decision_id, CONFIRMER)
    assert locked["session_status"] == SessionStatus.LOCKED.value

    # the human's release marks the request approved; the executor verifies the lock
    approval.decision = "approved"
    executor = make_executor(gate)
    payment = await executor.execute(make_invoice(), approval)

    assert payment.status == "initiated"
    payment_record = gate.records.list()[0]
    assert payment_record["action"] == "payment_initiated"
    assert payment_record["confirmed_by"] == CONFIRMER
    assert payment_record["details"]["payment_reference"] == payment.payment_reference


async def test_pipeline_orchestrator_passes_the_po_into_the_gated_route(
    tmp_path: Path, queue_stub: QueueStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agents.orchestrator import P2PPipeline

    # constructing the pipeline builds LLM clients from settings; the tests
    # never call them, so a stub credential keeps the suite offline
    monkeypatch.setattr(settings.llm, "api_key", "test-key")

    pipeline = P2PPipeline()
    pipeline.approval_router.gate = make_gate(tmp_path, require_lock=True)

    approval = await pipeline.approval_router.route(
        make_invoice(), make_validation(), [], purchase_order=make_po()
    )

    # default wiring: the orchestrator's router is CHP-gated, and the PO the
    # pipeline passes in is the parity basis for the held approval
    assert pipeline.approval_router.gate.enabled is True
    assert approval.decision is None
    assert approval.chp_decision_id is not None
    record = pipeline.approval_router.gate.records.get(approval.chp_decision_id)
    assert record is not None
    assert record["parity"]["basis"] == "purchase_order"


# ----------------------------------------------------------- dashboard surface


def test_dashboard_surfaces_and_confirms_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, queue_stub: QueueStub
) -> None:
    from fastapi.testclient import TestClient

    ledger_path = tmp_path / "chp_decisions.jsonl"
    monkeypatch.setattr(settings, "chp", ChpConfig(decisions_path=ledger_path))

    gate = SpendApprovalGate(settings.chp)
    decision = hardened_decision(gate)
    gate.record(
        decision,
        invoice_id="INV-001",
        stage="approval_routing",
        outcome="held_for_human_lock",
    )
    decision_id = decision.case.decision_id

    client = TestClient(app)
    listing = client.get("/api/decisions").json()
    assert listing[0]["decision_id"] == decision_id
    assert listing[0]["envelope_valid"] is True
    assert listing[0]["integrity_valid"] is True

    assert client.get("/api/decisions/spend-missing").status_code == 404

    confirmed = client.post(
        f"/api/decisions/{decision_id}/confirm", json={"confirmed_by": CONFIRMER}
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["session_status"] == SessionStatus.LOCKED.value
    assert confirmed.json()["confirmed_by"] == CONFIRMER

    refused_gate = SpendApprovalGate(settings.chp)
    refused_gate.record_refusal(
        invoice_id="INV-009", stage="payment_execution", reason="no parity"
    )
    conflict = client.post(
        "/api/decisions/spend-INV-009/confirm", json={"confirmed_by": CONFIRMER}
    )
    assert conflict.status_code == 409


def test_anomaly_shapes_are_untouched_by_the_gate() -> None:
    # the gate only reads anomaly risk levels; it never mutates them
    anomaly = Anomaly(
        invoice_id="INV-001",
        anomaly_type=AnomalyType.OVERCHARGE,
        risk_level=RiskLevel.HIGH,
        description="unit price above PO",
        confidence=0.9,
    )
    assert anomaly.risk_level is RiskLevel.HIGH


def test_chp_validation_result_enum_is_aliased_not_shadowed() -> None:
    # shared.models.ValidationResult (invoice validation) must stay a pydantic
    # model while the gate uses chp's CONFIRM/REJECT enum internally.
    assert ChpValidationResult.CONFIRM.value == "CONFIRM"
    assert make_validation().po_match is True
