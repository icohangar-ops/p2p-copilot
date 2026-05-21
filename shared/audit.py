"""Audit trail logging for full P2P traceability."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from shared.config import settings
from shared.models import AuditEntry

logger = structlog.get_logger(__name__)


class AuditTrail:
    def __init__(self, log_path: Path | None = None) -> None:
        self.log_path = log_path or settings.audit_log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        stage: str,
        invoice_id: str,
        action: str,
        actor: str = "system",
        details: dict[str, Any] | None = None,
        decision: str | None = None,
        confidence: float | None = None,
    ) -> AuditEntry:
        entry = AuditEntry(
            timestamp=datetime.utcnow(),
            stage=stage,
            invoice_id=invoice_id,
            action=action,
            actor=actor,
            details=details or {},
            decision=decision,
            confidence=confidence,
        )
        self._persist(entry)
        logger.info(
            "audit_log",
            stage=stage,
            invoice_id=invoice_id,
            action=action,
            actor=actor,
            decision=decision,
        )
        return entry

    def _persist(self, entry: AuditEntry) -> None:
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")

    def query(
        self,
        invoice_id: str | None = None,
        stage: str | None = None,
        limit: int = 100,
    ) -> list[AuditEntry]:
        entries: list[AuditEntry] = []
        if not self.log_path.exists():
            return entries
        with open(self.log_path, encoding="utf-8") as f:
            for line in f:
                entry = AuditEntry.model_validate(json.loads(line.strip()))
                if invoice_id and entry.invoice_id != invoice_id:
                    continue
                if stage and entry.stage != stage:
                    continue
                entries.append(entry)
                if len(entries) >= limit:
                    break
        return entries


audit = AuditTrail()
