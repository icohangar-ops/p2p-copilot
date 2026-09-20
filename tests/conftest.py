"""Shared test fixtures: keep suite side effects out of the repo's data/."""

import pytest

from shared.audit import audit


@pytest.fixture(autouse=True)
def isolate_audit_log(tmp_path, monkeypatch):
    """Redirect the audit singleton so tests never append to data/audit_log.jsonl."""
    monkeypatch.setattr(audit, "log_path", tmp_path / "audit.jsonl")
