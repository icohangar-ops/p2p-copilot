"""Stage 1: Invoice Intake — OCR extraction and structuring via Claude Vision."""

from __future__ import annotations

import base64
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic

from shared.audit import audit
from shared.config import settings
from shared.models import Invoice, InvoiceStatus, LineItem

EXTRACTION_PROMPT = """You are an expert accounts payable clerk. Extract structured invoice data from this document image.

Return a JSON object with these exact fields:
{
  "vendor_name": "string",
  "vendor_id": "string or null",
  "invoice_number": "string",
  "invoice_date": "YYYY-MM-DD",
  "due_date": "YYYY-MM-DD or null",
  "currency": "3-letter code",
  "subtotal": "decimal string",
  "tax": "decimal string",
  "total": "decimal string",
  "po_number": "string or null",
  "line_items": [
    {
      "description": "string",
      "quantity": "decimal string",
      "unit_price": "decimal string",
      "total": "decimal string",
      "po_line_ref": "string or null"
    }
  ]
}

Be precise with numbers. If a field is not visible, use null. Return ONLY valid JSON."""


class InvoiceExtractor:
    def __init__(self) -> None:
        self.client = anthropic.Anthropic()
        self.model = settings.claude.model

    async def extract_from_file(self, file_path: Path) -> Invoice:
        image_data = base64.standard_b64encode(file_path.read_bytes()).decode("utf-8")
        media_type = self._detect_media_type(file_path)

        response = self.client.messages.create(
            model=self.model,
            max_tokens=settings.claude.max_tokens,
            temperature=settings.claude.temperature,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_data,
                            },
                        },
                        {"type": "text", "text": EXTRACTION_PROMPT},
                    ],
                }
            ],
        )

        raw_json = response.content[0].text
        parsed = self._parse_response(raw_json)
        invoice = self._to_invoice(parsed, source=str(file_path))

        audit.log(
            stage="invoice_intake",
            invoice_id=invoice.invoice_id,
            action="extracted",
            actor="claude_vision",
            details={"source": str(file_path), "confidence": invoice.confidence_score},
        )
        return invoice

    async def extract_from_text(self, raw_text: str) -> Invoice:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=settings.claude.max_tokens,
            temperature=settings.claude.temperature,
            messages=[
                {
                    "role": "user",
                    "content": f"{EXTRACTION_PROMPT}\n\nInvoice text:\n{raw_text}",
                }
            ],
        )

        raw_json = response.content[0].text
        parsed = self._parse_response(raw_json)
        invoice = self._to_invoice(parsed, source="email_text")
        invoice.raw_text = raw_text

        audit.log(
            stage="invoice_intake",
            invoice_id=invoice.invoice_id,
            action="extracted_from_text",
            actor="claude",
        )
        return invoice

    def _detect_media_type(self, path: Path) -> str:
        suffix = path.suffix.lower()
        return {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".pdf": "application/pdf",
        }.get(suffix, "image/png")

    def _parse_response(self, raw: str) -> dict[str, Any]:
        import json

        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(cleaned)

    def _to_invoice(self, data: dict[str, Any], source: str) -> Invoice:
        import uuid

        line_items = [
            LineItem(
                description=li["description"],
                quantity=Decimal(str(li["quantity"])),
                unit_price=Decimal(str(li["unit_price"])),
                total=Decimal(str(li["total"])),
                po_line_ref=li.get("po_line_ref"),
            )
            for li in data.get("line_items", [])
        ]

        return Invoice(
            invoice_id=f"INV-{uuid.uuid4().hex[:8].upper()}",
            vendor_name=data["vendor_name"],
            vendor_id=data.get("vendor_id"),
            invoice_number=data["invoice_number"],
            invoice_date=data["invoice_date"],
            due_date=data.get("due_date"),
            currency=data.get("currency", "USD"),
            subtotal=Decimal(str(data["subtotal"])),
            tax=Decimal(str(data.get("tax", "0"))),
            total=Decimal(str(data["total"])),
            line_items=line_items,
            po_number=data.get("po_number"),
            status=InvoiceStatus.EXTRACTED,
            source=source,
            confidence_score=0.95,
        )
