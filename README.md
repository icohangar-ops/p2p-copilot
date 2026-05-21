# P2P Copilot — AI-Powered Procure-to-Pay on UiPath Maestro

> **UiPath AgentHack 2026 | Track 2: Maestro BPMN**

An end-to-end AI-powered Procure-to-Pay pipeline that orchestrates invoice processing from intake through payment using UiPath Maestro BPMN, Claude AI, and Python ML — eliminating manual AP workflows while maintaining full audit traceability.

## The Problem

Enterprise Accounts Payable teams manually process thousands of invoices monthly, leading to:
- **3-5% duplicate payment rate** costing enterprises millions annually
- **15+ days** average invoice processing cycle time
- **60%+** of AP staff time spent on manual data entry and verification
- No real-time visibility into payment pipeline status
- Audit trail gaps that create compliance risk

## How It Works

P2P Copilot models the entire procure-to-pay lifecycle as a BPMN 2.0 process orchestrated through UiPath Maestro, with AI agents handling each decision point:

```
Invoice Received → OCR Extract → AI Validate → Anomaly Detect → Route Approval → Execute Payment → Audit Log
     📄              🤖              🔍              🚨              👤              💰            📊
   Stage 1         Stage 2        Stage 3         Stage 4        Stage 5        Stage 6
```

### Stage 1: Invoice Intake (UiPath Studio + Claude Vision)
- Auto-ingests invoices from email, vendor portals, or direct upload
- Claude Vision API extracts structured data from PDF/image invoices
- Falls back to human-in-the-loop via UiPath Action Center for low-confidence extractions

### Stage 2: AI Validation (Maestro Agent + Claude API)
- Cross-checks invoice against Purchase Order (PO), goods receipt, and contract terms
- Claude LLM performs semantic matching — catches issues rigid rules miss
- Validates line items, quantities, pricing, dates, and vendor identity

### Stage 3: Anomaly Detection (Python ML + Business Rules)
- Scikit-learn isolation forest model trained on historical invoice patterns
- Rules engine flags: duplicates, overcharges, missing POs, vendor mismatches, date anomalies
- Configurable thresholds via `business_rules.yaml`

### Stage 4: Approval Routing (UiPath Orchestrator Queues + Action Center)
- Dynamic routing matrix based on invoice amount, department, and risk level
- Auto-approve: < $5K with low risk → instant payment
- Risk escalation: anomalies bump approval up 1-2 levels automatically
- UiPath Action Center delivers approval tasks to the right person

### Stage 5: Payment Execution (UiPath Studio + Banking API)
- Generates NACHA ACH batch files or wire instructions
- Posts transactions to ERP general ledger via API
- Payment confirmation loop with automatic retry

### Stage 6: Audit Trail Dashboard (FastAPI)
- Real-time dashboard showing every decision in the pipeline
- Per-invoice timeline: who/what/when/why at each stage
- AI confidence scores logged for every automated decision
- Full traceability for SOX/internal audit compliance

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    UiPath Maestro (BPMN 2.0)                    │
│                  Orchestrates the full P2P flow                 │
├─────────┬──────────┬────────────┬───────────┬──────────┬───────┤
│ Stage 1 │ Stage 2  │  Stage 3   │  Stage 4  │ Stage 5  │  S6   │
│ Intake  │ Validate │  Anomaly   │  Approve  │ Payment  │ Audit │
│         │          │  Detect    │  Route    │ Execute  │ Trail │
├─────────┼──────────┼────────────┼───────────┼──────────┼───────┤
│ Claude  │ Claude   │ scikit-    │ Orch.     │ Studio   │ Fast- │
│ Vision  │ API      │ learn +    │ Queues +  │ + Bank   │ API   │
│ + OCR   │ + PO     │ Rules      │ Actions   │ API      │       │
│         │ Match    │ Engine     │ Center    │          │       │
└─────────┴──────────┴────────────┴───────────┴──────────┴───────┘
```

## UiPath Components Used

| Component | Usage |
|-----------|-------|
| **Maestro (BPMN)** | End-to-end process orchestration with BPMN 2.0 diagram |
| **Studio (REFramework)** | Invoice intake automation, payment file generation |
| **Orchestrator** | Queue management for approval routing, job scheduling |
| **Action Center** | Human-in-the-loop approval tasks for managers/directors/CFO |
| **AI Center** | Document Understanding for OCR preprocessing |
| **API Workflows** | Webhook triggers for real-time invoice ingestion |

## Coding Agents

This project was built with **Claude Code** (Anthropic's coding agent) for:
- Full codebase scaffolding and architecture design
- Python agent implementation (all 6 pipeline stages)
- BPMN 2.0 process definition
- Test suite creation
- Dashboard development

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- UiPath Automation Cloud account with Maestro enabled
- Anthropic API key (for Claude Vision + LLM validation)
- UiPath Studio 2024.10+ (for RPA workflows)

## Setup

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/p2p-copilot.git
cd p2p-copilot

# Install Python dependencies
uv sync

# Configure environment
cp .env.example .env
# Edit .env with your UiPath and Anthropic credentials

# Run tests
uv run pytest

# Start the audit dashboard
uv run uvicorn dashboard.app:app --reload --port 8000
```

### UiPath Setup

1. Import the BPMN process: `architecture/p2p-process.bpmn` into UiPath Maestro
2. Create Orchestrator queues: `P2P_Invoices`, `P2P_Approvals`, `P2P_Payments`
3. Deploy Studio workflows from `uipath/` folder to Orchestrator
4. Configure API webhook triggers for invoice intake

## Project Structure

```
p2p-copilot/
├── agents/
│   ├── invoice_intake/       # Stage 1: OCR extraction via Claude Vision
│   ├── invoice_validator/    # Stage 2: AI cross-validation against PO/contract
│   ├── anomaly_detector/     # Stage 3: ML + rules anomaly detection
│   ├── approval_router/      # Stage 4: Dynamic approval routing
│   ├── payment_executor/     # Stage 5: Payment file generation
│   └── orchestrator.py       # End-to-end pipeline coordinator
├── shared/
│   ├── models.py             # Pydantic data models (Invoice, PO, Anomaly, etc.)
│   ├── config.py             # Environment-based configuration
│   ├── audit.py              # Audit trail persistence
│   └── uipath_client.py      # UiPath Orchestrator API client
├── dashboard/
│   └── app.py                # Stage 6: FastAPI audit trail dashboard
├── architecture/
│   └── p2p-process.bpmn      # BPMN 2.0 process definition for Maestro
├── tests/                    # Pytest test suite
├── data/                     # Sample invoices and POs for testing
├── pyproject.toml            # Python project config (uv)
└── .env.example              # Environment template
```

## License

MIT License - Copyright (c) 2026 Shyam Desigan
