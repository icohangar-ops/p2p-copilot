"""Tests for P2P data models."""

from datetime import date
from decimal import Decimal

from shared.models import (
    Anomaly,
    AnomalyType,
    ApprovalRequest,
    Invoice,
    InvoiceStatus,
    LineItem,
    PurchaseOrder,
    RiskLevel,
    ValidationResult,
)


def test_invoice_creation() -> None:
    invoice = Invoice(
        invoice_id="INV-TEST001",
        vendor_name="Acme Corp",
        invoice_number="AC-2026-001",
        invoice_date=date(2026, 5, 1),
        subtotal=Decimal("1000.00"),
        tax=Decimal("80.00"),
        total=Decimal("1080.00"),
    )
    assert invoice.status == InvoiceStatus.RECEIVED
    assert invoice.currency == "USD"
    assert invoice.total == Decimal("1080.00")


def test_invoice_with_line_items() -> None:
    items = [
        LineItem(
            description="Widget A",
            quantity=Decimal("10"),
            unit_price=Decimal("50.00"),
            total=Decimal("500.00"),
        ),
        LineItem(
            description="Widget B",
            quantity=Decimal("5"),
            unit_price=Decimal("100.00"),
            total=Decimal("500.00"),
        ),
    ]
    invoice = Invoice(
        invoice_id="INV-TEST002",
        vendor_name="Widgets Inc",
        invoice_number="WI-001",
        invoice_date=date(2026, 5, 15),
        subtotal=Decimal("1000.00"),
        total=Decimal("1000.00"),
        line_items=items,
    )
    assert len(invoice.line_items) == 2
    assert sum(li.total for li in invoice.line_items) == invoice.subtotal


def test_purchase_order() -> None:
    po = PurchaseOrder(
        po_number="PO-2026-100",
        vendor_id="V-001",
        vendor_name="Acme Corp",
        order_date=date(2026, 4, 1),
        total_amount=Decimal("5000.00"),
    )
    assert po.status == "open"
    assert po.total_amount == Decimal("5000.00")


def test_validation_result() -> None:
    result = ValidationResult(
        invoice_id="INV-TEST001",
        is_valid=True,
        confidence=0.95,
        po_match=True,
    )
    assert result.is_valid
    assert result.confidence == 0.95
    assert result.issues == []


def test_anomaly() -> None:
    anomaly = Anomaly(
        invoice_id="INV-TEST001",
        anomaly_type=AnomalyType.OVERCHARGE,
        risk_level=RiskLevel.HIGH,
        description="Invoice exceeds PO by 15%",
        expected_value="5000.00",
        actual_value="5750.00",
        confidence=0.92,
    )
    assert anomaly.risk_level == RiskLevel.HIGH
    assert anomaly.anomaly_type == AnomalyType.OVERCHARGE


def test_approval_request() -> None:
    request = ApprovalRequest(
        invoice_id="INV-TEST001",
        amount=Decimal("50000.00"),
        department="Engineering",
        vendor_name="Acme Corp",
        assigned_to="finance_director",
        risk_level=RiskLevel.MEDIUM,
    )
    assert request.decision is None
    assert request.assigned_to == "finance_director"
