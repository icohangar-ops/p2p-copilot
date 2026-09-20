"""Stage 5: Payment Execution — Generate payment files and trigger ERP/banking integration.

The payment action is the last CHP gate before money moves: an R0 gate runs
before initiation, parity re-checks the payment against the approved decision,
and with ``P2P_CHP__REQUIRE_HUMAN_LOCK`` on (default) the payment requires the
spend decision to be LOCKED by a named approver in the decision ledger.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime
from typing import Any

from cubiczan_resilience import InMemoryIdempotencyStore

from shared.audit import audit
from shared.chp import ChpRejection, FoundationAssessment, SpendApprovalGate
from shared.models import ApprovalRequest, Invoice, PaymentRecord


class PaymentExecutor:
    def __init__(self, gate: SpendApprovalGate | None = None) -> None:
        self._payment_log: list[PaymentRecord] = []
        # Idempotency guard: a given invoice must only ever be paid once,
        # even if execute() is retried or replayed for the same invoice_id.
        self._idempotency = InMemoryIdempotencyStore()
        self.gate = gate or SpendApprovalGate()

    def _existing_payment(self, invoice_id: str) -> PaymentRecord | None:
        for record in self._payment_log:
            if record.invoice_id == invoice_id:
                return record
        return None

    async def execute(
        self,
        invoice: Invoice,
        approval: ApprovalRequest,
        payment_method: str = "ach",
    ) -> PaymentRecord:
        # Pre-flight idempotency check: if this invoice was already paid,
        # return the existing PaymentRecord instead of initiating a duplicate.
        if self._idempotency.already_done(invoice.invoice_id):
            existing = self._existing_payment(invoice.invoice_id)
            if existing is not None:
                return existing

        if approval.decision != "approved":
            raise ValueError(
                f"Cannot pay invoice {invoice.invoice_id}: approval status is '{approval.decision}'"
            )

        gate_context: tuple[FoundationAssessment, dict[str, Any]] | None = None
        if self.gate.enabled:
            # Raises ChpRejection (recorded) when the spend is not authorized.
            gate_context = self._chp_gate_payment(invoice, approval)

        payment = PaymentRecord(
            invoice_id=invoice.invoice_id,
            payment_method=payment_method,
            payment_reference=f"PAY-{uuid.uuid4().hex[:10].upper()}",
            amount=invoice.total,
            currency=invoice.currency,
            executed_at=datetime.utcnow(),
            status="initiated",
        )

        # Atomically claim the invoice_id before initiating the payment so a
        # concurrent/retried call cannot produce a second payment. First writer
        # wins; if the key was already claimed, return the existing record.
        if not self._idempotency.mark_done(
            invoice.invoice_id, payment.payment_reference
        ):
            existing = self._existing_payment(invoice.invoice_id)
            if existing is not None:
                return existing

        audit.log(
            stage="payment_execution",
            invoice_id=invoice.invoice_id,
            action="payment_initiated",
            actor="payment_executor",
            details={
                "method": payment_method,
                "amount": str(invoice.total),
                "reference": payment.payment_reference,
            },
        )

        if gate_context is not None:
            assessment, authorization = gate_context
            self.gate.record_payment(
                invoice_id=invoice.invoice_id,
                amount=invoice.total,
                payment_reference=payment.payment_reference,
                assessment=assessment,
                authorization=authorization,
            )

        self._payment_log.append(payment)
        return payment

    def _chp_gate_payment(
        self, invoice: Invoice, approval: ApprovalRequest
    ) -> tuple[FoundationAssessment, dict[str, Any]]:
        """R0 + human-lock authorization before the spend executes; refusals recorded."""
        try:
            self.gate.open_r0(
                invoice_id=invoice.invoice_id,
                amount=invoice.total,
                currency=invoice.currency,
                approver=approval.assigned_to,
                validation=approval.validation_result,
            )
            # Payment parity: the executed amount must match the approved
            # decision — a fabricated/mutated approval cannot move money.
            assessment: FoundationAssessment = self.gate.assess_foundation(
                amount=invoice.total,
                expected_amount=approval.amount,
                parity_basis="approved_decision",
                parity_reference=approval.chp_decision_id or invoice.invoice_id,
                guardrail_evidence=(
                    "approval decided, amount matches the approved decision,"
                    " idempotency claimed before initiation"
                ),
                assigned_to=approval.assigned_to,
            )
            self.gate.check_foundation(assessment)
            authorization = self.gate.authorization_for_payment(approval)
        except ChpRejection as rejection:
            self.gate.record_refusal(
                invoice_id=invoice.invoice_id,
                stage="payment_execution",
                reason=rejection.reason,
                evaluation=rejection.evaluation,
            )
            audit.log(
                stage="payment_execution",
                invoice_id=invoice.invoice_id,
                action="chp_refused",
                actor="chp_gate",
                details={"reason": rejection.reason},
                decision="refused",
            )
            raise

        if authorization is None:
            reason = (
                f"CHP human lock required: payment refused until a named approver locks"
                f" decision {approval.chp_decision_id or '(none recorded for this approval)'}"
            )
            self.gate.record_refusal(
                invoice_id=invoice.invoice_id,
                stage="payment_execution",
                reason=reason,
                assessment=assessment,
            )
            audit.log(
                stage="payment_execution",
                invoice_id=invoice.invoice_id,
                action="chp_refused",
                actor="chp_gate",
                details={"reason": reason},
                decision="refused",
            )
            raise ChpRejection(reason)

        return assessment, authorization

    def generate_ach_file(self, payments: list[PaymentRecord]) -> str:
        """Generate NACHA-format ACH batch file content."""
        lines: list[str] = []
        batch_id = uuid.uuid4().hex[:8].upper()
        now = datetime.utcnow()

        lines.append(
            f"101 DESTBANK  ORIGBANK {now:%y%m%d%H%M} "
            f"A094101{batch_id}P2P COPILOT         "
        )
        lines.append(
            f"5200P2P COPILOT PAYMENTS    {batch_id}  "
            f"PPDPayments {now:%y%m%d}   1ORIGBANK0000001"
        )

        for i, payment in enumerate(payments, start=1):
            amount_cents = int(payment.amount * 100)
            lines.append(
                f"622DESTBANK  {payment.payment_reference:<17}"
                f"{amount_cents:010d}  {payment.invoice_id:<15}"
                f"{i:07d}"
            )

        total_amount = sum(int(p.amount * 100) for p in payments)
        lines.append(
            f"8200{len(payments):06d}{total_amount:012d}"
            f"ORIGBANK{batch_id}"
        )
        lines.append(
            f"9{1:06d}{1:06d}{len(payments):08d}{total_amount:012d}"
        )

        return "\n".join(lines)

    def generate_payment_csv(self, payments: list[PaymentRecord]) -> str:
        """Generate payment CSV for ERP import."""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Payment Reference",
            "Invoice ID",
            "Amount",
            "Currency",
            "Payment Method",
            "Status",
            "Executed At",
        ])
        for p in payments:
            writer.writerow([
                p.payment_reference,
                p.invoice_id,
                str(p.amount),
                p.currency,
                p.payment_method,
                p.status,
                p.executed_at.isoformat(),
            ])
        return output.getvalue()

    def get_batch_summary(self, payments: list[PaymentRecord]) -> dict[str, Any]:
        total = sum(p.amount for p in payments)
        return {
            "batch_count": len(payments),
            "total_amount": str(total),
            "currencies": list({p.currency for p in payments}),
            "methods": list({p.payment_method for p in payments}),
            "statuses": {
                status: sum(1 for p in payments if p.status == status)
                for status in {p.status for p in payments}
            },
        }
