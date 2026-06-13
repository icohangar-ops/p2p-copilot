"""Stage 5: Payment Execution — Generate payment files and trigger ERP/banking integration."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from cubiczan_resilience import InMemoryIdempotencyStore

from shared.audit import audit
from shared.models import ApprovalRequest, Invoice, PaymentRecord


class PaymentExecutor:
    def __init__(self) -> None:
        self._payment_log: list[PaymentRecord] = []
        # Idempotency guard: a given invoice must only ever be paid once,
        # even if execute() is retried or replayed for the same invoice_id.
        self._idempotency = InMemoryIdempotencyStore()

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

        self._payment_log.append(payment)
        return payment

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
