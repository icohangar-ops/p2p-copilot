"""Pydantic data models for the P2P pipeline."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class InvoiceStatus(StrEnum):
    RECEIVED = "received"
    EXTRACTED = "extracted"
    VALIDATED = "validated"
    FLAGGED = "flagged"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAID = "paid"


class AnomalyType(StrEnum):
    DUPLICATE = "duplicate"
    OVERCHARGE = "overcharge"
    MISSING_PO = "missing_po"
    AMOUNT_MISMATCH = "amount_mismatch"
    VENDOR_MISMATCH = "vendor_mismatch"
    DATE_ANOMALY = "date_anomaly"
    QUANTITY_MISMATCH = "quantity_mismatch"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class LineItem(BaseModel):
    description: str
    quantity: Decimal
    unit_price: Decimal
    total: Decimal
    po_line_ref: str | None = None


class Invoice(BaseModel):
    invoice_id: str
    vendor_name: str
    vendor_id: str | None = None
    invoice_number: str
    invoice_date: date
    due_date: date | None = None
    currency: str = "USD"
    subtotal: Decimal
    tax: Decimal = Decimal("0")
    total: Decimal
    line_items: list[LineItem] = Field(default_factory=list)
    po_number: str | None = None
    status: InvoiceStatus = InvoiceStatus.RECEIVED
    source: str = "upload"
    raw_text: str | None = None
    confidence_score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PurchaseOrder(BaseModel):
    po_number: str
    vendor_id: str
    vendor_name: str
    order_date: date
    expected_delivery: date | None = None
    currency: str = "USD"
    total_amount: Decimal
    line_items: list[LineItem] = Field(default_factory=list)
    department: str | None = None
    approver: str | None = None
    status: str = "open"


class ValidationResult(BaseModel):
    invoice_id: str
    is_valid: bool
    confidence: float = Field(ge=0.0, le=1.0)
    po_match: bool = False
    receipt_match: bool = False
    contract_compliant: bool = False
    issues: list[str] = Field(default_factory=list)
    llm_reasoning: str | None = None
    validated_at: datetime = Field(default_factory=datetime.utcnow)


class Anomaly(BaseModel):
    invoice_id: str
    anomaly_type: AnomalyType
    risk_level: RiskLevel
    description: str
    expected_value: str | None = None
    actual_value: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    detected_at: datetime = Field(default_factory=datetime.utcnow)


class ApprovalRequest(BaseModel):
    invoice_id: str
    amount: Decimal
    department: str
    vendor_name: str
    assigned_to: str
    risk_level: RiskLevel
    anomalies: list[Anomaly] = Field(default_factory=list)
    validation_result: ValidationResult | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    decided_at: datetime | None = None
    decision: str | None = None
    decision_reason: str | None = None


class PaymentRecord(BaseModel):
    invoice_id: str
    payment_method: str
    payment_reference: str
    amount: Decimal
    currency: str = "USD"
    executed_at: datetime = Field(default_factory=datetime.utcnow)
    status: str = "pending"
    erp_transaction_id: str | None = None


class AuditEntry(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    stage: str
    invoice_id: str
    action: str
    actor: str
    details: dict[str, Any] = Field(default_factory=dict)
    decision: str | None = None
    confidence: float | None = None
