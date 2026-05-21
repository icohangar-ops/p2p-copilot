"""Configuration management for P2P Copilot."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class UiPathConfig(BaseModel):
    cloud_url: str = "https://cloud.uipath.com"
    org_name: str = ""
    tenant_name: str = ""
    client_id: str = ""
    client_secret: str = ""
    orchestrator_folder: str = "P2P-Copilot"


class ClaudeConfig(BaseModel):
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 4096
    temperature: float = 0.1


class ApprovalPolicy(BaseModel):
    auto_approve_threshold: float = 5000.00
    manager_approve_threshold: float = 25000.00
    vp_approve_threshold: float = 100000.00
    risk_override: bool = True


class Settings(BaseSettings):
    model_config = {"env_prefix": "P2P_", "env_file": ".env", "env_nested_delimiter": "__", "extra": "ignore"}

    app_name: str = "P2P Copilot"
    debug: bool = False
    log_level: str = "INFO"

    uipath: UiPathConfig = Field(default_factory=UiPathConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    approval_policy: ApprovalPolicy = Field(default_factory=ApprovalPolicy)

    data_dir: Path = Path("data")
    audit_log_path: Path = Path("data/audit_log.jsonl")


settings = Settings()
