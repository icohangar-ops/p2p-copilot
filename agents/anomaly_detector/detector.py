"""Stage 3: Anomaly Detection — ML + rules-based invoice anomaly detection."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import yaml

from shared.audit import audit
from shared.models import Anomaly, AnomalyType, Invoice, PurchaseOrder, RiskLevel


class AnomalyDetector:
    def __init__(self, rules_path: str = "agents/anomaly_detector/rules/business_rules.yaml") -> None:
        self.rules = self._load_rules(rules_path)
        self._seen_invoices: dict[str, Invoice] = {}

    def detect(
        self,
        invoice: Invoice,
        purchase_order: PurchaseOrder | None = None,
        historical_invoices: list[Invoice] | None = None,
    ) -> list[Anomaly]:
        anomalies: list[Anomaly] = []

        anomalies.extend(self._check_duplicates(invoice, historical_invoices or []))
        anomalies.extend(self._check_amount_anomalies(invoice, purchase_order))
        anomalies.extend(self._check_vendor_anomalies(invoice, purchase_order))
        anomalies.extend(self._check_date_anomalies(invoice))
        anomalies.extend(self._check_quantity_anomalies(invoice, purchase_order))

        for anomaly in anomalies:
            audit.log(
                stage="anomaly_detection",
                invoice_id=invoice.invoice_id,
                action=f"anomaly_detected:{anomaly.anomaly_type}",
                actor="anomaly_detector",
                details={
                    "type": anomaly.anomaly_type,
                    "risk": anomaly.risk_level,
                    "description": anomaly.description,
                },
                confidence=anomaly.confidence,
            )

        self._seen_invoices[invoice.invoice_number] = invoice
        return anomalies

    def _check_duplicates(
        self, invoice: Invoice, historical: list[Invoice]
    ) -> list[Anomaly]:
        anomalies: list[Anomaly] = []
        all_invoices = list(historical) + list(self._seen_invoices.values())

        for existing in all_invoices:
            if existing.invoice_id == invoice.invoice_id:
                continue

            if (
                existing.invoice_number == invoice.invoice_number
                and existing.vendor_name == invoice.vendor_name
            ):
                anomalies.append(
                    Anomaly(
                        invoice_id=invoice.invoice_id,
                        anomaly_type=AnomalyType.DUPLICATE,
                        risk_level=RiskLevel.CRITICAL,
                        description=f"Exact duplicate: invoice {invoice.invoice_number} from {invoice.vendor_name}",
                        confidence=0.99,
                    )
                )

            elif (
                existing.vendor_name == invoice.vendor_name
                and existing.total == invoice.total
                and abs((existing.invoice_date - invoice.invoice_date).days) < 7
            ):
                anomalies.append(
                    Anomaly(
                        invoice_id=invoice.invoice_id,
                        anomaly_type=AnomalyType.DUPLICATE,
                        risk_level=RiskLevel.HIGH,
                        description=(
                            f"Potential duplicate: same vendor, amount ${invoice.total}, "
                            f"within 7 days of {existing.invoice_number}"
                        ),
                        confidence=0.75,
                    )
                )

        return anomalies

    def _check_amount_anomalies(
        self, invoice: Invoice, po: PurchaseOrder | None
    ) -> list[Anomaly]:
        anomalies: list[Anomaly] = []
        threshold = Decimal(str(self.rules.get("overcharge_threshold_pct", 10)))

        if po:
            if invoice.total > po.total_amount:
                overage_pct = (
                    (invoice.total - po.total_amount) / po.total_amount * 100
                )
                risk = RiskLevel.CRITICAL if overage_pct > threshold else RiskLevel.HIGH
                anomalies.append(
                    Anomaly(
                        invoice_id=invoice.invoice_id,
                        anomaly_type=AnomalyType.OVERCHARGE,
                        risk_level=risk,
                        description=f"Invoice ${invoice.total} exceeds PO ${po.total_amount} by {overage_pct:.1f}%",
                        expected_value=str(po.total_amount),
                        actual_value=str(invoice.total),
                        confidence=0.95,
                    )
                )

        if not invoice.po_number:
            anomalies.append(
                Anomaly(
                    invoice_id=invoice.invoice_id,
                    anomaly_type=AnomalyType.MISSING_PO,
                    risk_level=RiskLevel.MEDIUM,
                    description="Invoice has no PO reference",
                    confidence=1.0,
                )
            )

        return anomalies

    def _check_vendor_anomalies(
        self, invoice: Invoice, po: PurchaseOrder | None
    ) -> list[Anomaly]:
        if not po:
            return []
        anomalies: list[Anomaly] = []

        if invoice.vendor_name.lower().strip() != po.vendor_name.lower().strip():
            anomalies.append(
                Anomaly(
                    invoice_id=invoice.invoice_id,
                    anomaly_type=AnomalyType.VENDOR_MISMATCH,
                    risk_level=RiskLevel.HIGH,
                    description=f"Vendor mismatch: invoice='{invoice.vendor_name}', PO='{po.vendor_name}'",
                    expected_value=po.vendor_name,
                    actual_value=invoice.vendor_name,
                    confidence=0.9,
                )
            )

        return anomalies

    def _check_date_anomalies(self, invoice: Invoice) -> list[Anomaly]:
        anomalies: list[Anomaly] = []

        if invoice.due_date and invoice.due_date < invoice.invoice_date:
            anomalies.append(
                Anomaly(
                    invoice_id=invoice.invoice_id,
                    anomaly_type=AnomalyType.DATE_ANOMALY,
                    risk_level=RiskLevel.HIGH,
                    description="Due date is before invoice date",
                    expected_value=f"after {invoice.invoice_date}",
                    actual_value=str(invoice.due_date),
                    confidence=0.99,
                )
            )

        return anomalies

    def _check_quantity_anomalies(
        self, invoice: Invoice, po: PurchaseOrder | None
    ) -> list[Anomaly]:
        if not po or not po.line_items or not invoice.line_items:
            return []

        anomalies: list[Anomaly] = []
        tolerance = Decimal(str(self.rules.get("quantity_tolerance_pct", 5)))

        po_items = {li.description.lower(): li for li in po.line_items}

        for inv_item in invoice.line_items:
            po_item = po_items.get(inv_item.description.lower())
            if not po_item:
                continue

            if po_item.quantity > 0:
                diff_pct = abs(inv_item.quantity - po_item.quantity) / po_item.quantity * 100
                if diff_pct > tolerance:
                    anomalies.append(
                        Anomaly(
                            invoice_id=invoice.invoice_id,
                            anomaly_type=AnomalyType.QUANTITY_MISMATCH,
                            risk_level=RiskLevel.MEDIUM,
                            description=(
                                f"Quantity mismatch for '{inv_item.description}': "
                                f"invoice={inv_item.quantity}, PO={po_item.quantity} ({diff_pct:.1f}% diff)"
                            ),
                            expected_value=str(po_item.quantity),
                            actual_value=str(inv_item.quantity),
                            confidence=0.9,
                        )
                    )

        return anomalies

    def _load_rules(self, path: str) -> dict[str, Any]:
        try:
            with open(path, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except FileNotFoundError:
            return {
                "overcharge_threshold_pct": 10,
                "quantity_tolerance_pct": 5,
                "duplicate_window_days": 7,
            }
