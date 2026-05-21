"""P2P Pipeline Orchestrator — Coordinates all 6 stages end-to-end."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from agents.anomaly_detector.detector import AnomalyDetector
from agents.approval_router.router import ApprovalRouter
from agents.invoice_intake.extractor import InvoiceExtractor
from agents.invoice_validator.validator import InvoiceValidator
from agents.payment_executor.executor import PaymentExecutor
from shared.audit import audit
from shared.models import (
    Anomaly,
    ApprovalRequest,
    Invoice,
    InvoiceStatus,
    PaymentRecord,
    PurchaseOrder,
    ValidationResult,
)

logger = structlog.get_logger(__name__)


class P2PPipeline:
    def __init__(self) -> None:
        self.extractor = InvoiceExtractor()
        self.validator = InvoiceValidator()
        self.anomaly_detector = AnomalyDetector()
        self.approval_router = ApprovalRouter()
        self.payment_executor = PaymentExecutor()

    async def process_invoice(
        self,
        file_path: Path | None = None,
        raw_text: str | None = None,
        purchase_order: PurchaseOrder | None = None,
        contract_terms: dict[str, Any] | None = None,
        historical_invoices: list[Invoice] | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"stages": {}}

        # Stage 1: Invoice Intake
        logger.info("pipeline_stage", stage="invoice_intake")
        if file_path:
            invoice = await self.extractor.extract_from_file(file_path)
        elif raw_text:
            invoice = await self.extractor.extract_from_text(raw_text)
        else:
            raise ValueError("Either file_path or raw_text is required")
        result["invoice"] = invoice
        result["stages"]["intake"] = {"status": "complete", "invoice_id": invoice.invoice_id}

        # Stage 2: AI Validation
        logger.info("pipeline_stage", stage="ai_validation", invoice_id=invoice.invoice_id)
        validation = await self.validator.validate(
            invoice, purchase_order, contract_terms
        )
        invoice.status = InvoiceStatus.VALIDATED if validation.is_valid else InvoiceStatus.FLAGGED
        result["validation"] = validation
        result["stages"]["validation"] = {
            "status": "complete",
            "is_valid": validation.is_valid,
            "issues": validation.issues,
        }

        # Stage 3: Anomaly Detection
        logger.info("pipeline_stage", stage="anomaly_detection", invoice_id=invoice.invoice_id)
        anomalies = self.anomaly_detector.detect(
            invoice, purchase_order, historical_invoices or []
        )
        result["anomalies"] = anomalies
        result["stages"]["anomaly_detection"] = {
            "status": "complete",
            "anomaly_count": len(anomalies),
            "risk_levels": [a.risk_level for a in anomalies],
        }

        # Stage 4: Approval Routing
        logger.info("pipeline_stage", stage="approval_routing", invoice_id=invoice.invoice_id)
        approval = await self.approval_router.route(invoice, validation, anomalies)
        result["approval"] = approval
        result["stages"]["approval"] = {
            "status": "complete",
            "assigned_to": approval.assigned_to,
            "decision": approval.decision,
        }

        # Stage 5: Payment Execution (only if approved)
        if approval.decision == "approved":
            logger.info("pipeline_stage", stage="payment_execution", invoice_id=invoice.invoice_id)
            payment = await self.payment_executor.execute(invoice, approval)
            invoice.status = InvoiceStatus.PAID
            result["payment"] = payment
            result["stages"]["payment"] = {
                "status": "complete",
                "reference": payment.payment_reference,
            }
        else:
            result["stages"]["payment"] = {
                "status": "pending_approval",
                "assigned_to": approval.assigned_to,
            }

        # Stage 6: Audit summary
        result["stages"]["audit"] = {"status": "complete", "dashboard_url": "/"}
        result["pipeline_status"] = "complete"

        logger.info(
            "pipeline_complete",
            invoice_id=invoice.invoice_id,
            final_status=invoice.status,
            anomaly_count=len(anomalies),
            decision=approval.decision,
        )
        return result
