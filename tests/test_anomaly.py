"""Tests for anomaly detection engine."""

from datetime import date
from decimal import Decimal

from agents.anomaly_detector.detector import AnomalyDetector
from shared.models import (
    AnomalyType,
    Invoice,
    InvoiceStatus,
    LineItem,
    PurchaseOrder,
    RiskLevel,
)


def _make_invoice(
    invoice_id: str = "INV-001",
    vendor: str = "Acme Corp",
    number: str = "AC-001",
    total: str = "1000.00",
    invoice_date: date = date(2026, 5, 1),
    po_number: str | None = "PO-100",
) -> Invoice:
    return Invoice(
        invoice_id=invoice_id,
        vendor_name=vendor,
        invoice_number=number,
        invoice_date=invoice_date,
        subtotal=Decimal(total),
        total=Decimal(total),
        po_number=po_number,
        status=InvoiceStatus.EXTRACTED,
    )


def _make_po(
    po_number: str = "PO-100",
    vendor: str = "Acme Corp",
    total: str = "1000.00",
) -> PurchaseOrder:
    return PurchaseOrder(
        po_number=po_number,
        vendor_id="V-001",
        vendor_name=vendor,
        order_date=date(2026, 4, 1),
        total_amount=Decimal(total),
    )


def test_no_anomalies() -> None:
    detector = AnomalyDetector()
    invoice = _make_invoice()
    po = _make_po()
    anomalies = detector.detect(invoice, po)
    assert len(anomalies) == 0


def test_duplicate_detection() -> None:
    detector = AnomalyDetector()
    inv1 = _make_invoice(invoice_id="INV-001")
    inv2 = _make_invoice(invoice_id="INV-002")

    detector.detect(inv1)
    anomalies = detector.detect(inv2, historical_invoices=[inv1])

    dupes = [a for a in anomalies if a.anomaly_type == AnomalyType.DUPLICATE]
    assert len(dupes) >= 1
    assert dupes[0].risk_level == RiskLevel.CRITICAL


def test_overcharge_detection() -> None:
    detector = AnomalyDetector()
    invoice = _make_invoice(total="1200.00")
    po = _make_po(total="1000.00")
    anomalies = detector.detect(invoice, po)

    overcharges = [a for a in anomalies if a.anomaly_type == AnomalyType.OVERCHARGE]
    assert len(overcharges) == 1
    assert overcharges[0].risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)


def test_missing_po_detection() -> None:
    detector = AnomalyDetector()
    invoice = _make_invoice(po_number=None)
    anomalies = detector.detect(invoice)

    missing = [a for a in anomalies if a.anomaly_type == AnomalyType.MISSING_PO]
    assert len(missing) == 1


def test_vendor_mismatch() -> None:
    detector = AnomalyDetector()
    invoice = _make_invoice(vendor="Acme Corporation")
    po = _make_po(vendor="Beta Industries")
    anomalies = detector.detect(invoice, po)

    mismatches = [a for a in anomalies if a.anomaly_type == AnomalyType.VENDOR_MISMATCH]
    assert len(mismatches) == 1


def test_date_anomaly() -> None:
    detector = AnomalyDetector()
    invoice = Invoice(
        invoice_id="INV-DATE",
        vendor_name="Acme",
        invoice_number="AC-DATE",
        invoice_date=date(2026, 5, 15),
        due_date=date(2026, 5, 1),
        subtotal=Decimal("100"),
        total=Decimal("100"),
        po_number="PO-100",
    )
    anomalies = detector.detect(invoice)

    date_issues = [a for a in anomalies if a.anomaly_type == AnomalyType.DATE_ANOMALY]
    assert len(date_issues) == 1


def test_quantity_mismatch() -> None:
    detector = AnomalyDetector()
    invoice = _make_invoice()
    invoice.line_items = [
        LineItem(description="Widget A", quantity=Decimal("20"), unit_price=Decimal("50"), total=Decimal("1000"))
    ]
    po = _make_po()
    po.line_items = [
        LineItem(description="Widget A", quantity=Decimal("10"), unit_price=Decimal("50"), total=Decimal("500"))
    ]
    anomalies = detector.detect(invoice, po)

    qty_issues = [a for a in anomalies if a.anomaly_type == AnomalyType.QUANTITY_MISMATCH]
    assert len(qty_issues) == 1
