"""Stage 6: Audit Trail Dashboard — FastAPI-based P2P visibility and traceability."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from shared.audit import audit
from shared.config import settings
from shared.models import AuditEntry

app = FastAPI(
    title="P2P Copilot Dashboard",
    description="AI-Powered Procure-to-Pay audit trail and monitoring",
    version="0.1.0",
)


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>P2P Copilot Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
               background: #0f172a; color: #e2e8f0; }
        .header { background: linear-gradient(135deg, #1e3a5f, #0f172a);
                   padding: 2rem; border-bottom: 1px solid #334155; }
        .header h1 { font-size: 1.8rem; color: #38bdf8; }
        .header p { color: #94a3b8; margin-top: 0.5rem; }
        .container { max-width: 1400px; margin: 0 auto; padding: 2rem; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
                 gap: 1.5rem; margin-bottom: 2rem; }
        .card { background: #1e293b; border-radius: 12px; padding: 1.5rem;
                 border: 1px solid #334155; }
        .card h3 { color: #38bdf8; margin-bottom: 1rem; font-size: 0.9rem;
                    text-transform: uppercase; letter-spacing: 0.05em; }
        .metric { font-size: 2.5rem; font-weight: 700; color: #f8fafc; }
        .metric-label { color: #94a3b8; font-size: 0.85rem; margin-top: 0.25rem; }
        .table-container { overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; }
        th { text-align: left; padding: 0.75rem 1rem; color: #94a3b8;
             border-bottom: 1px solid #334155; font-size: 0.8rem;
             text-transform: uppercase; letter-spacing: 0.05em; }
        td { padding: 0.75rem 1rem; border-bottom: 1px solid #1e293b; font-size: 0.9rem; }
        .badge { padding: 0.25rem 0.75rem; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; }
        .badge-green { background: #065f46; color: #34d399; }
        .badge-yellow { background: #713f12; color: #fbbf24; }
        .badge-red { background: #7f1d1d; color: #f87171; }
        .badge-blue { background: #1e3a5f; color: #38bdf8; }
        #audit-log { margin-top: 1rem; }
        .refresh-btn { background: #38bdf8; color: #0f172a; border: none; padding: 0.5rem 1rem;
                        border-radius: 6px; cursor: pointer; font-weight: 600; }
        .refresh-btn:hover { background: #7dd3fc; }
    </style>
</head>
<body>
    <div class="header">
        <h1>P2P Copilot Dashboard</h1>
        <p>AI-Powered Procure-to-Pay Monitoring & Audit Trail</p>
    </div>
    <div class="container">
        <div class="grid" id="metrics"></div>
        <div class="card">
            <div style="display:flex;justify-content:space-between;align-items:center;">
                <h3>Audit Trail</h3>
                <button class="refresh-btn" onclick="loadData()">Refresh</button>
            </div>
            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th>Timestamp</th><th>Stage</th><th>Invoice</th>
                            <th>Action</th><th>Actor</th><th>Decision</th><th>Confidence</th>
                        </tr>
                    </thead>
                    <tbody id="audit-log"></tbody>
                </table>
            </div>
        </div>
    </div>
    <script>
        async function loadData() {
            const [statsRes, logRes] = await Promise.all([
                fetch('/api/stats'),
                fetch('/api/audit?limit=50')
            ]);
            const stats = await statsRes.json();
            const logs = await logRes.json();
            renderMetrics(stats);
            renderLog(logs);
        }
        function renderMetrics(s) {
            document.getElementById('metrics').innerHTML = `
                <div class="card"><h3>Total Invoices</h3><div class="metric">${s.total_entries}</div></div>
                <div class="card"><h3>Stages Active</h3><div class="metric">${Object.keys(s.by_stage).length}</div></div>
                <div class="card"><h3>Anomalies Detected</h3>
                    <div class="metric">${s.by_action?.anomaly_detected || 0}</div></div>
                <div class="card"><h3>Auto-Approved</h3>
                    <div class="metric">${s.by_action?.auto_approved || 0}</div></div>
            `;
        }
        function badgeClass(stage) {
            const m = {invoice_intake:'badge-blue',ai_validation:'badge-green',
                       anomaly_detection:'badge-yellow',approval_routing:'badge-blue',
                       payment_execution:'badge-green'};
            return m[stage] || 'badge-blue';
        }
        function renderLog(entries) {
            document.getElementById('audit-log').innerHTML = entries.map(e => `
                <tr>
                    <td>${new Date(e.timestamp).toLocaleString()}</td>
                    <td><span class="badge ${badgeClass(e.stage)}">${e.stage}</span></td>
                    <td>${e.invoice_id}</td>
                    <td>${e.action}</td>
                    <td>${e.actor}</td>
                    <td>${e.decision || '-'}</td>
                    <td>${e.confidence ? (e.confidence * 100).toFixed(0) + '%' : '-'}</td>
                </tr>
            `).join('');
        }
        loadData();
        setInterval(loadData, 10000);
    </script>
</body>
</html>"""


@app.get("/api/audit")
async def get_audit_log(
    invoice_id: str | None = Query(None),
    stage: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
) -> list[dict]:
    entries = audit.query(invoice_id=invoice_id, stage=stage, limit=limit)
    return [e.model_dump(mode="json") for e in entries]


@app.get("/api/stats")
async def get_stats() -> dict[str, Any]:
    entries = audit.query(limit=10000)
    by_stage: dict[str, int] = {}
    by_action: dict[str, int] = {}
    invoices: set[str] = set()

    for e in entries:
        by_stage[e.stage] = by_stage.get(e.stage, 0) + 1
        action_key = e.action.split(":")[0]
        by_action[action_key] = by_action.get(action_key, 0) + 1
        invoices.add(e.invoice_id)

    return {
        "total_entries": len(entries),
        "unique_invoices": len(invoices),
        "by_stage": by_stage,
        "by_action": by_action,
    }


@app.get("/api/invoice/{invoice_id}/timeline")
async def get_invoice_timeline(invoice_id: str) -> list[dict]:
    entries = audit.query(invoice_id=invoice_id, limit=500)
    if not entries:
        raise HTTPException(status_code=404, detail=f"No audit entries for {invoice_id}")
    return [e.model_dump(mode="json") for e in entries]
