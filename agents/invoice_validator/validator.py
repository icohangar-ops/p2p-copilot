"""Stage 2: AI Validation — Cross-check invoice against PO, receipt, contract terms."""

from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from cubiczan_resilience import resilient

from shared.audit import audit
from shared.config import settings
from shared.models import Invoice, PurchaseOrder, ValidationResult

VALIDATION_PROMPT = """You are a senior AP auditor. Compare this invoice against the purchase order and flag discrepancies.

INVOICE:
{invoice_json}

PURCHASE ORDER:
{po_json}

Check for:
1. Vendor name/ID match
2. PO number match
3. Line item quantities match (within 5% tolerance)
4. Unit prices match exactly
5. Total amounts reconcile (subtotal + tax = total)
6. Currency matches
7. Invoice date is after PO date
8. Delivery within expected window

Return JSON:
{{
  "is_valid": true/false,
  "confidence": 0.0-1.0,
  "po_match": true/false,
  "issues": ["list of specific discrepancies found"],
  "reasoning": "brief explanation of your assessment"
}}

Return ONLY valid JSON."""


class InvoiceValidator:
    def __init__(self) -> None:
        self.client = AsyncOpenAI(
            api_key=settings.llm.api_key,
            base_url=settings.llm.base_url or None,
        )
        self.model = settings.llm.model

    @resilient(timeout=60.0, max_attempts=3)
    async def _chat(self, prompt: str) -> Any:
        """Timeout + retry-wrapped LLM call (non-blocking via AsyncOpenAI)."""
        return await self.client.chat.completions.create(
            model=self.model,
            max_tokens=settings.llm.max_tokens,
            temperature=settings.llm.temperature,
            messages=[{"role": "user", "content": prompt}],
        )

    async def validate(
        self,
        invoice: Invoice,
        purchase_order: PurchaseOrder | None = None,
        contract_terms: dict[str, Any] | None = None,
    ) -> ValidationResult:
        if purchase_order:
            result = await self._validate_against_po(invoice, purchase_order)
        else:
            result = self._validate_standalone(invoice)

        if contract_terms:
            contract_issues = self._check_contract_terms(invoice, contract_terms)
            result.issues.extend(contract_issues)
            result.contract_compliant = len(contract_issues) == 0
            if contract_issues:
                result.is_valid = False

        audit.log(
            stage="ai_validation",
            invoice_id=invoice.invoice_id,
            action="validated",
            actor="gpt4o_validator",
            details={
                "is_valid": result.is_valid,
                "issues_count": len(result.issues),
                "po_match": result.po_match,
            },
            confidence=result.confidence,
        )
        return result

    async def _validate_against_po(
        self, invoice: Invoice, po: PurchaseOrder
    ) -> ValidationResult:
        prompt = VALIDATION_PROMPT.format(
            invoice_json=invoice.model_dump_json(indent=2),
            po_json=po.model_dump_json(indent=2),
        )

        response = await self._chat(prompt)

        parsed = self._parse_response(response.choices[0].message.content)
        return ValidationResult(
            invoice_id=invoice.invoice_id,
            is_valid=parsed["is_valid"],
            confidence=parsed["confidence"],
            po_match=parsed["po_match"],
            issues=parsed.get("issues", []),
            llm_reasoning=parsed.get("reasoning"),
        )

    def _validate_standalone(self, invoice: Invoice) -> ValidationResult:
        issues: list[str] = []

        if not invoice.po_number:
            issues.append("No PO number referenced on invoice")

        expected_total = invoice.subtotal + invoice.tax
        if abs(expected_total - invoice.total) > 0.01:
            issues.append(
                f"Total mismatch: subtotal({invoice.subtotal}) + tax({invoice.tax}) "
                f"!= total({invoice.total})"
            )

        line_total = sum(li.total for li in invoice.line_items)
        if invoice.line_items and abs(line_total - invoice.subtotal) > 0.01:
            issues.append(
                f"Line items total({line_total}) != subtotal({invoice.subtotal})"
            )

        return ValidationResult(
            invoice_id=invoice.invoice_id,
            is_valid=len(issues) == 0,
            confidence=0.7 if issues else 0.85,
            issues=issues,
        )

    def _check_contract_terms(
        self, invoice: Invoice, terms: dict[str, Any]
    ) -> list[str]:
        issues: list[str] = []

        max_amount = terms.get("max_invoice_amount")
        if max_amount and invoice.total > max_amount:
            issues.append(
                f"Invoice total {invoice.total} exceeds contract max {max_amount}"
            )

        payment_terms = terms.get("payment_terms_days")
        if payment_terms and invoice.due_date and invoice.invoice_date:
            days = (invoice.due_date - invoice.invoice_date).days
            if days < payment_terms:
                issues.append(
                    f"Payment terms {days}d shorter than contract {payment_terms}d"
                )

        return issues

    def _parse_response(self, raw: str) -> dict[str, Any]:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(cleaned)
