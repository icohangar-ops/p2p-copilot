"""Stage 5: Payment Execution — Generate payment files and trigger ERP/banking integration."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from shared.audit import audit
from shared.models import ApprovalRequest, Invoice, PaymentRecord


class PaymentExecutor:
    def __init__(self) -> None:
        self._payment_log: list[PaymentRecord] = []

    async def execute(
        self,
        invoice: Invoice,
        approval: ApprovalRequest,
        payment_method: str = "ach",
    ) -> PaymentRecord:
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
