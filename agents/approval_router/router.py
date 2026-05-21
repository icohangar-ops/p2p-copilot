"""Stage 4: Approval Routing — Dynamic routing based on amount, department, policy, risk."""

from __future__ import annotations

from decimal import Decimal

from shared.audit import audit
from shared.config import settings
from shared.models import (
    Anomaly,
    ApprovalRequest,
    Invoice,
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

    async def route(
        self,
        invoice: Invoice,
        validation: ValidationResult,
        anomalies: list[Anomaly],
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

        if final_approver == "auto_approve" and risk_level in (
            RiskLevel.LOW,
            RiskLevel.MEDIUM,
        ):
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
        else:
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
        await uipath.add_queue_item(
            queue_name="P2P_Approvals",
            data={
                "InvoiceId": request.invoice_id,
                "Amount": str(request.amount),
                "Department": request.department,
                "VendorName": request.vendor_name,
                "AssignedTo": request.assigned_to,
                "RiskLevel": request.risk_level,
                "AnomalyCount": len(request.anomalies),
            },
            priority="High" if request.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL) else "Normal",
        )
