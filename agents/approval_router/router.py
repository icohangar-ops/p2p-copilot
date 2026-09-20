"""Stage 4: Approval Routing — Dynamic routing based on amount, department, policy, risk.

The auto-approve branch is CHP-hardened: an R0 gate runs before the decision,
the deterministic adversary scores the approval's foundation (guardrails +
bounded decision state + PO parity), and the case opens PROVISIONAL_LOCK.
Spend approvals are capital_allocation decisions gating at the CHP finance
floor of 100, so only a parity-verified approval can self-certify; with
``P2P_CHP__REQUIRE_HUMAN_LOCK`` on (default) every auto-approval is held for a
named human lock instead.
"""

from __future__ import annotations

from decimal import Decimal

from shared.audit import audit
from shared.chp import ChpRejection, SpendApprovalGate, SpendDecision
from shared.config import settings
from shared.models import (
    Anomaly,
    ApprovalRequest,
    Invoice,
    PurchaseOrder,
    RiskLevel,
    ValidationResult,
)
from shared.uipath_client import uipath

APPROVAL_MATRIX: dict[str, list[dict]] = {
    "default": [
        {"max_amount": 5_000, "approver": "auto_approve", "level": "auto"},
        {"max_amount": 25_000, "approver": "department_manager", "level": "L1"},
        {"max_amount": 100_000, "approver": "finance_director", "level": "L2"},
        {"max_amount": 500_000, "approver": "cfo", "level": "L3"},
        {"max_amount": float("inf"), "approver": "ceo", "level": "L4"},
    ],
}

RISK_ESCALATION: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 0,
    RiskLevel.HIGH: 1,
    RiskLevel.CRITICAL: 2,
}


class ApprovalRouter:
    def __init__(self) -> None:
        self.policy = settings.approval_policy
        self.gate = SpendApprovalGate()

    async def route(
        self,
        invoice: Invoice,
        validation: ValidationResult,
        anomalies: list[Anomaly],
        purchase_order: PurchaseOrder | None = None,
    ) -> ApprovalRequest:
        risk_level = self._assess_risk(anomalies)
        department = invoice.metadata.get("department", "general")
        base_approver, base_level = self._determine_approver(
            invoice.total, department
        )

        escalation = RISK_ESCALATION.get(risk_level, 0)
        final_approver, final_level = self._escalate(
            base_approver, base_level, escalation, department
        )

        request = ApprovalRequest(
            invoice_id=invoice.invoice_id,
            amount=invoice.total,
            department=department,
            vendor_name=invoice.vendor_name,
            assigned_to=final_approver,
            risk_level=risk_level,
            anomalies=anomalies,
            validation_result=validation,
        )

        auto_eligible = final_approver == "auto_approve" and risk_level in (
            RiskLevel.LOW,
            RiskLevel.MEDIUM,
        )

        if auto_eligible and self.gate.enabled:
            # R0 before the approval decision — failures are fatal and recorded.
            self._open_r0(invoice, validation, purchase_order, final_approver)
            decision = self._harden(invoice, validation, purchase_order, final_approver)
            return await self._resolve_auto_approval(
                request, invoice, decision, final_level
            )

        if auto_eligible:
            # CHP gate disabled: legacy auto-approval, no human lock.
            request.decision = "approved"
            request.decision_reason = "Auto-approved: below threshold, low risk"
            audit.log(
                stage="approval_routing",
                invoice_id=invoice.invoice_id,
                action="auto_approved",
                actor="system",
                details={"amount": str(invoice.total), "risk": risk_level},
                decision="approved",
            )
            return request

        await self._send_to_queue(request)
        audit.log(
            stage="approval_routing",
            invoice_id=invoice.invoice_id,
            action="routed_for_approval",
            actor="system",
            details={
                "assigned_to": final_approver,
                "level": final_level,
                "risk": risk_level,
                "escalated": escalation > 0,
            },
        )

        return request

    def _open_r0(
        self,
        invoice: Invoice,
        validation: ValidationResult,
        purchase_order: PurchaseOrder | None,
        approver: str,
    ) -> None:
        """R0 gate before the approval decision; HALT is fatal and recorded."""
        try:
            self.gate.open_r0(
                invoice_id=invoice.invoice_id,
                amount=invoice.total,
                currency=invoice.currency,
                approver=approver,
                validation=validation,
                purchase_order=purchase_order,
                auto_approve=True,
                auto_approve_bound=Decimal(str(self.policy.auto_approve_threshold)),
            )
        except ChpRejection as rejection:
            self.gate.record_refusal(
                invoice_id=invoice.invoice_id,
                stage="approval_routing",
                reason=rejection.reason,
                evaluation=rejection.evaluation,
            )
            audit.log(
                stage="approval_routing",
                invoice_id=invoice.invoice_id,
                action="chp_refused",
                actor="chp_r0_gate",
                details={"reason": rejection.reason},
                decision="refused",
            )
            raise

    def _harden(
        self,
        invoice: Invoice,
        validation: ValidationResult,
        purchase_order: PurchaseOrder | None,
        approver: str,
    ) -> SpendDecision:
        """Foundation pass + PROVISIONAL_LOCK; a parity mismatch is fatal."""
        try:
            return self.gate.harden(
                invoice_id=invoice.invoice_id,
                vendor_name=invoice.vendor_name,
                amount=invoice.total,
                currency=invoice.currency,
                validation=validation,
                purchase_order=purchase_order,
                approver=approver,
            )
        except ChpRejection as rejection:
            self.gate.record_refusal(
                invoice_id=invoice.invoice_id,
                stage="approval_routing",
                reason=rejection.reason,
            )
            audit.log(
                stage="approval_routing",
                invoice_id=invoice.invoice_id,
                action="chp_refused",
                actor="chp_foundation",
                details={"reason": rejection.reason},
                decision="refused",
            )
            raise

    async def _resolve_auto_approval(
        self,
        request: ApprovalRequest,
        invoice: Invoice,
        decision: SpendDecision,
        level: str,
    ) -> ApprovalRequest:
        """Decide the hardened auto-approval: hold for a human lock or self-certify."""
        invoice_id = invoice.invoice_id
        sub_floor = decision.assessment.score < self.gate.finance_floor

        if self.gate.require_human_lock or sub_floor:
            hold_reason = (
                "CHP human lock required: spend approvals never auto-approve"
                " without a named approver"
                if self.gate.require_human_lock
                else "CHP finance floor not met without parity evidence: the"
                " approval cannot self-certify and needs a named approver"
            )
            record = self.gate.record(
                decision,
                invoice_id=invoice_id,
                stage="approval_routing",
                outcome="held_for_human_lock",
                refusal_reason=hold_reason,
                details={
                    "amount": str(invoice.total),
                    "assigned_to": request.assigned_to,
                    "level": level,
                },
            )
            request.chp_decision_id = record["decision_id"]
            request.decision = None
            request.decision_reason = f"{hold_reason} (decision {record['decision_id']})"
            audit.log(
                stage="approval_routing",
                invoice_id=invoice_id,
                action="chp_held_for_human",
                actor="chp_gate",
                details={
                    "decision_id": record["decision_id"],
                    "foundation_score": record["foundation_score"],
                    "reason": hold_reason,
                },
                decision="held_for_human_lock",
            )
            await self._send_to_queue(request)
            return request

        record = self.gate.record(
            decision,
            invoice_id=invoice_id,
            stage="approval_routing",
            outcome="auto_approved",
            details={
                "amount": str(invoice.total),
                "assigned_to": request.assigned_to,
                "level": level,
            },
        )
        request.chp_decision_id = record["decision_id"]
        request.decision = "approved"
        request.decision_reason = (
            "Auto-approved: CHP foundation passed the finance floor with"
            f" parity evidence (decision {record['decision_id']})"
        )
        audit.log(
            stage="approval_routing",
            invoice_id=invoice_id,
            action="auto_approved",
            actor="chp_gate",
            details={
                "amount": str(invoice.total),
                "risk": request.risk_level,
                "decision_id": record["decision_id"],
                "foundation_score": record["foundation_score"],
            },
            decision="approved",
        )
        return request

    def _assess_risk(self, anomalies: list[Anomaly]) -> RiskLevel:
        if not anomalies:
            return RiskLevel.LOW

        risk_order = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]
        max_risk = max(
            (risk_order.index(a.risk_level) for a in anomalies),
            default=0,
        )
        return risk_order[max_risk]

    def _determine_approver(
        self, amount: Decimal, department: str
    ) -> tuple[str, str]:
        matrix = APPROVAL_MATRIX.get(department, APPROVAL_MATRIX["default"])
        for tier in matrix:
            if amount <= tier["max_amount"]:
                return tier["approver"], tier["level"]
        return "ceo", "L4"

    def _escalate(
        self,
        approver: str,
        level: str,
        steps: int,
        department: str,
    ) -> tuple[str, str]:
        if steps == 0:
            return approver, level

        matrix = APPROVAL_MATRIX.get(department, APPROVAL_MATRIX["default"])
        current_idx = next(
            (i for i, t in enumerate(matrix) if t["approver"] == approver), 0
        )
        escalated_idx = min(current_idx + steps, len(matrix) - 1)
        tier = matrix[escalated_idx]
        return tier["approver"], tier["level"]

    async def _send_to_queue(self, request: ApprovalRequest) -> None:
        data = {
            "InvoiceId": request.invoice_id,
            "Amount": str(request.amount),
            "Department": request.department,
            "VendorName": request.vendor_name,
            "AssignedTo": request.assigned_to,
            "RiskLevel": request.risk_level,
            "AnomalyCount": len(request.anomalies),
        }
        if request.chp_decision_id:
            data["ChpDecisionId"] = request.chp_decision_id
        await uipath.add_queue_item(
            queue_name="P2P_Approvals",
            data=data,
            priority=(
                "High" if request.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL) else "Normal"
            ),
        )
