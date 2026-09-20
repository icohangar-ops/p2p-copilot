"""CHP-hardened spend-approval gate (consensus-hardening-protocol, Profile A).

Every payment / PO approval decision becomes a CHP decision case, so the audit
question "why was invoice INV-1 paid?" has a mechanical answer. Four hardening
stages wrap the P2P approval path, ported from the pattern proven in
erp-control-plane commit 70678cc (api/genbi/chp.py):

1. **R0 gate — before the decision.** ``SpendApprovalGate.open_r0`` asks whether
   the approval is *solvable* (invoice, amount, and approver are resolvable),
   *scoped* (currency known, amount finite, and an auto-approval bounded by the
   matrix tier it acts within), *valid* (a validation result backs the request,
   and any attached PO is well-formed), and *worth_it* (real spend above zero).
   HALT refuses the decision with nothing approved or paid — R0 failures are
   fatal.
2. **Foundation pass — the deterministic adversary.** The approval's foundation
   scores out of 100: 40 for guardrails (the deterministic approval matrix /
   payment preconditions held — no LLM output decided the spend), 30 for a
   bounded decision state (amount resolved to an accountable approver), and 30
   for parity — the billed amount matching the pinned ordered truth (the PO
   record at approval time, the approved decision at payment time), three-way-
   match style. Spend approvals are ``capital_allocation`` decisions and gate at
   CHP's finance floor (100): without parity evidence the foundation cannot
   self-certify, and the approval needs a named human approver. A parity
   *mismatch* is fatal — an approval contradicting what was ordered must not
   stand, and no confirmer can wave it through.
3. **Human lock.** A hardened case opens ``PROVISIONAL_LOCK``; when a named
   approver confirms (``confirmed_by``), CHP third-party validation locks it
   (``LOCKED``). ``P2P_CHP__REQUIRE_HUMAN_LOCK`` (default on) makes that
   confirmation mandatory for every payment — spend must never auto-approve.
4. **Decision record.** The case, verdicts, parity evidence, and payment
   details are sealed into a CHP payload envelope and appended to the decision
   ledger (append-only JSONL). The CHP envelope validates structure only, so
   the ledger adds its own SHA-256 digest over the sealed body and re-validates
   both on read — a tampered record surfaces as ``integrity_valid: false``.

Approvals **and** refusals are recorded: R0 halts, parity mismatches, sub-floor
holds, and lock-required holds all land in the ledger with a reason.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from chp import (
    CHPOrchestrator,
    CHPReport,
    DecisionCase,
    Dossier,
    FoundationAttack,
    FoundationDisclosure,
    SessionStatus,
    ThirdPartyValidation,
    Verdict,
    apply_third_party_validation,
    build_payload_envelope,
    validate_payload_envelope,
)
from chp import ValidationResult as ChpValidationResult
from chp.foundation import foundation_floor
from chp.gates import GateEvaluation, evaluate_r0_gate

from shared.config import ChpConfig, settings
from shared.models import ApprovalRequest

logger = logging.getLogger(__name__)

# Deterministic adversary scoring (out of 100). The capital_allocation floor in
# chp.foundation is exactly 100, so only a parity-verified approval can
# self-certify a spend decision.
_GUARDRAIL_POINTS = 40
_BOUNDED_DECISION_POINTS = 30
_PARITY_POINTS = 30
_FULL_SCORE = _GUARDRAIL_POINTS + _BOUNDED_DECISION_POINTS + _PARITY_POINTS

# Spend approvals are capital-allocation decisions: CHP gates the domain at
# floor 100. A lookalike domain string would silently gate at the general
# floor of 70, so the exact key matters.
_SPEND_DOMAIN = "capital_allocation"

# Currency parity tolerance: one cent.
_AMOUNT_TOLERANCE = Decimal("0.01")

_ENVELOPE_ROUTE = "SPEND_APPROVAL"


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


class ChpRejection(Exception):
    """CHP refused the spend decision (R0 HALT, foundation REFRAME, or lock required)."""

    def __init__(self, reason: str, evaluation: GateEvaluation | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.evaluation = evaluation


@dataclass(frozen=True)
class ParityEvidence:
    """The billed amount vs the pinned truth for what was ordered/approved."""

    basis: str  # "purchase_order" | "approved_decision"
    reference: str  # PO number / approval decision id
    expected: str
    actual: str
    tolerance: str
    within_tolerance: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FoundationAssessment:
    """The deterministic adversary's verdict on a spend decision."""

    score: int
    domain: str
    findings: list[str] = field(default_factory=list)
    parity: ParityEvidence | None = None


@dataclass(frozen=True)
class SpendDecision:
    """A hardened spend approval: the CHP case, its report, and the assessment."""

    case: DecisionCase
    report: CHPReport
    assessment: FoundationAssessment


class SpendDecisionLedger:
    """Append-only JSONL of CHP spend decisions; integrity re-checked on read."""

    def __init__(self, path: Any) -> None:
        self.path = path
        self._lock = threading.Lock()

    def append(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def _read_all(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self.path.exists():
                return []
            lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        """Newest-first records with envelope and body integrity re-validated."""
        return [self._checked(entry) for entry in self._read_all()[-limit:]][::-1]

    def get(self, decision_id: str) -> dict[str, Any] | None:
        for entry in reversed(self._read_all()):
            if entry.get("decision_id") == decision_id:
                return self._checked(entry)
        return None

    @staticmethod
    def _checked(entry: dict[str, Any]) -> dict[str, Any]:
        """Re-validate a record on read: envelope structure and body digest.

        The CHP payload envelope validates structure only, so the ledger adds
        its own SHA-256 digest over the sealed body — a tampered record reads
        as ``integrity_valid: false``.
        """
        body = entry.get("body", "")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        return {
            **entry,
            "envelope_valid": validate_payload_envelope(entry.get("envelope", "")),
            "integrity_valid": digest == entry.get("body_sha256"),
        }


class SpendApprovalGate:
    """Runs a spend decision through CHP: R0 -> foundation -> human lock -> record."""

    def __init__(self, config: ChpConfig | None = None) -> None:
        self.config = config or settings.chp
        self.records = SpendDecisionLedger(self.config.decisions_path)
        self.finance_floor = foundation_floor(_SPEND_DOMAIN)

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def require_human_lock(self) -> bool:
        return self.config.require_human_lock

    # ------------------------------------------------------------------- R0
    def open_r0(
        self,
        *,
        invoice_id: str,
        amount: Decimal,
        currency: str,
        approver: str,
        validation: Any,
        purchase_order: Any = None,
        auto_approve: bool = False,
        auto_approve_bound: Decimal | None = None,
    ) -> GateEvaluation:
        """The pre-decision gate: HALT before any approval or payment executes."""
        evaluation = evaluate_r0_gate(
            solvable=bool(invoice_id.strip())
            and amount > 0
            and bool(approver.strip()),
            scoped=bool(currency.strip())
            and amount.is_finite()
            and (
                not auto_approve
                or (auto_approve_bound is not None and amount <= auto_approve_bound)
            ),
            valid=validation is not None
            and (
                purchase_order is None
                or (bool(purchase_order.po_number.strip()) and purchase_order.total_amount > 0)
            ),
            worth_it=amount > 0,
        )
        if evaluation.verdict != Verdict.PASS:
            failed = [name for name, result in evaluation.results.items() if result != "PASS"]
            raise ChpRejection(
                "CHP R0 gate: the spend decision failed " + ", ".join(sorted(failed)),
                evaluation,
            )
        return evaluation

    # ------------------------------------------------------------ foundation
    def assess_foundation(
        self,
        *,
        amount: Decimal,
        expected_amount: Decimal | None,
        parity_basis: str,
        parity_reference: str,
        guardrail_evidence: str,
        assigned_to: str,
    ) -> FoundationAssessment:
        """The deterministic adversary scores the spend decision (0-100)."""
        findings: list[str] = [f"guardrails passed: {guardrail_evidence}"]
        score = _GUARDRAIL_POINTS

        if amount > 0 and assigned_to.strip():
            score += _BOUNDED_DECISION_POINTS
            findings.append(
                f"bounded decision state: amount {amount} resolved to approver '{assigned_to}'"
            )
        else:
            findings.append("decision state unresolved — no bounded amount/approver evidence")

        parity: ParityEvidence | None = None
        if expected_amount is None:
            findings.append(
                "no pinned amount backs this approval — parity evidence unavailable"
            )
        else:
            within = abs(amount - expected_amount) <= _AMOUNT_TOLERANCE
            parity = ParityEvidence(
                basis=parity_basis,
                reference=parity_reference,
                expected=str(expected_amount),
                actual=str(amount),
                tolerance=str(_AMOUNT_TOLERANCE),
                within_tolerance=within,
            )
            if within:
                score += _PARITY_POINTS
                findings.append(
                    f"parity: billed amount {amount} matches {parity_basis}"
                    f" {parity_reference} ({expected_amount})"
                )
            else:
                findings.append(
                    f"parity MISMATCH: billed amount {amount} contradicts {parity_basis}"
                    f" {parity_reference} ({expected_amount})"
                )

        return FoundationAssessment(
            score=min(score, _FULL_SCORE),
            domain=_SPEND_DOMAIN,
            findings=findings,
            parity=parity,
        )

    def check_foundation(self, assessment: FoundationAssessment) -> None:
        """Fatal on parity mismatch: no confirmer can wave a contradiction through."""
        if assessment.parity is not None and not assessment.parity.within_tolerance:
            raise ChpRejection(
                f"CHP foundation: {assessment.findings[-1]} — an approval contradicting the"
                " pinned amount must not stand; resolve the discrepancy before any spend."
            )

    # --------------------------------------------------------------- session
    def harden(
        self,
        *,
        invoice_id: str,
        vendor_name: str,
        amount: Decimal,
        currency: str,
        validation: Any,
        purchase_order: Any = None,
        approver: str = "",
    ) -> SpendDecision:
        """Run the CHP foundation pass and open the case as PROVISIONAL_LOCK."""
        expected = purchase_order.total_amount if purchase_order else None
        basis = "purchase_order" if purchase_order else "none"
        reference = purchase_order.po_number if purchase_order else invoice_id
        assessment = self.assess_foundation(
            amount=amount,
            expected_amount=expected,
            parity_basis=basis,
            parity_reference=reference,
            guardrail_evidence=(
                "deterministic approval matrix + risk escalation;"
                " no LLM output in the spend decision"
            ),
            assigned_to=approver,
        )
        self.check_foundation(assessment)

        case = DecisionCase(
            decision_id=f"spend-{invoice_id}",
            title=f"Spend approval: {vendor_name} {amount} {currency}",
            domain=_SPEND_DOMAIN,
            created_at=_now_iso(),
            owner="p2p-approval-router",
            high_stakes=True,
            dossier=Dossier(
                core_problem=(
                    f"Approve payment of {amount} {currency} to {vendor_name}"
                    f" (invoice {invoice_id})"
                ),
                goal_state=[
                    "pay only what was ordered and received, with a named accountable approver"
                ],
                current_state=[
                    f"validation: is_valid={validation.is_valid},"
                    f" confidence={validation.confidence:.2f}",
                    f"parity basis: {basis} {reference}",
                ],
                constraints=[
                    "approval matrix bound by amount tier and risk escalation",
                    f"CHP {_SPEND_DOMAIN} foundation floor {self.finance_floor}",
                    f"REQUIRE_HUMAN_LOCK={'on' if self.config.require_human_lock else 'off'}",
                ],
                scope=[f"invoice:{invoice_id}", f"parity_ref:{reference}"],
            ),
        )
        disclosure = FoundationDisclosure(
            weakest_assumptions=[
                "the pinned parity record is the ordered truth for this invoice",
                "the goods-receipt evidence reflects actual delivery",
                "the approval matrix amount tiers match current policy",
            ],
            invalidation_conditions=[
                "billed amount deviates from the pinned ordered total",
                "validation fails or anomalies escalate risk past the tier",
            ],
            key_vulnerability=(
                "single-source parity: only the pinned amount for"
                f" {basis} {reference}"
                if assessment.parity
                else "no parity evidence pins what this approval pays for"
            ),
        )
        # The adversary must address each disclosed weak assumption
        # (validate_foundation_pair requires min(3, len(assumptions)) attacks).
        attack = FoundationAttack(
            attack_summary="; ".join(assessment.findings),
            foundation_score=assessment.score,
            vulnerability_strike=(
                "without parity evidence the approval rests only on the routing policy,"
                " not on what was actually ordered"
            ),
            assumption_attacks=[
                "parity check of the billed amount against the pinned ordered total",
                "goods-receipt evidence pinned by the validation result",
                "deterministic matrix bounds every auto-approval server-side",
            ],
        )

        # Fresh orchestrator per case: the protocol registry is in-memory state
        # we do not rely on — the decision ledger is the durable record.
        report = CHPOrchestrator().run_initial_session(
            case=case, foundation_disclosure=disclosure, foundation_attack=attack
        )

        # The gate collapses CHP's multi-round phase flow into one approval
        # step: every hardened request opens as a provisional decision pending
        # human confirmation (which apply_third_party_validation then locks).
        # A REFRAME verdict keeps that status too — the approval may only
        # proceed through the same human lock, never self-certify.
        case.status = SessionStatus.PROVISIONAL_LOCK
        return SpendDecision(case, report, assessment)

    # ------------------------------------------------------------- human lock
    def lock(self, case: DecisionCase, confirmed_by: str) -> SessionStatus:
        """Third-party confirmation: PROVISIONAL_LOCK -> LOCKED (recorded in the case)."""
        return apply_third_party_validation(
            case,
            ThirdPartyValidation(
                validator=confirmed_by,
                item=case.decision_id,
                challenge="Confirm the spend approval matches the ordered/received truth",
                result=ChpValidationResult.CONFIRM,
                rationale="Named approver confirmed the spend via the P2P dashboard",
            ),
        )

    def reload_and_lock(self, decision_id: str, confirmed_by: str) -> dict[str, Any]:
        """Lock a held decision from its ledger record — the durable confirm path.

        The dashboard process does not hold the live DecisionCase, so the case
        is rebuilt from the newest ledger record (append-only keeps history)
        and the lock is appended as a new record for the same decision id.
        """
        entry = self.records.get(decision_id)
        if entry is None:
            raise ChpRejection(f"no CHP spend decision record for {decision_id}")
        if entry.get("action") == "refused":
            raise ChpRejection(
                f"CHP decision {decision_id} was refused"
                f" ({entry.get('refusal_reason')}); a refused decision cannot be confirmed"
            )
        if entry.get("session_status") == SessionStatus.LOCKED.value:
            return entry  # already locked — idempotent

        case = DecisionCase(
            decision_id=decision_id,
            title=entry.get("title", ""),
            domain=entry.get("domain", _SPEND_DOMAIN),
            created_at=entry.get("created_at") or _now_iso(),
            owner="p2p-approval-router",
            high_stakes=True,
            foundation_score=entry.get("foundation_score"),
        )
        case.status = SessionStatus.PROVISIONAL_LOCK
        status = self.lock(case, confirmed_by)

        body_state = dict(json.loads(entry["body"]))
        body_state.update(
            {
                "action": "spend_confirmed",
                "decision": "approved",
                "session_status": status.value,
                "confirmed_by": confirmed_by,
                "locked_decisions": list(case.locked_decisions),
            }
        )
        return self._seal_and_append(body_state)

    # ------------------------------------------------------- payment authorization
    def authorization_for_payment(self, approval: ApprovalRequest) -> dict[str, Any] | None:
        """The newest ledger record authorizing spend for this approval, or None."""
        if not approval.chp_decision_id:
            return None
        entry = self.records.get(approval.chp_decision_id)
        if entry is None or entry.get("action") == "refused":
            return None
        status = entry.get("session_status")
        if self.config.require_human_lock:
            if status == SessionStatus.LOCKED.value and entry.get("confirmed_by"):
                return entry
            return None
        if status == SessionStatus.LOCKED.value:
            return entry
        if status == SessionStatus.PROVISIONAL_LOCK.value and (
            entry.get("foundation_score") or 0
        ) >= self.finance_floor:
            return entry
        return None

    # ----------------------------------------------------------------- record
    def record(
        self,
        decision: SpendDecision,
        *,
        invoice_id: str,
        stage: str,
        outcome: str,
        confirmed_by: str | None = None,
        refusal_reason: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Seal the decision into a CHP payload envelope and append the ledger."""
        case = decision.case
        body_state = {
            "decision_id": case.decision_id,
            "created_at": case.created_at,
            "invoice_id": invoice_id,
            "stage": stage,
            "action": "spend_approval",
            "decision": outcome,
            "session_status": case.status.value,
            "r0_verdict": decision.report.r0_verdict.value,
            "foundation_verdict": decision.report.foundation_verdict.value,
            "foundation_score": (
                case.foundation_score
                if case.foundation_score is not None
                else decision.assessment.score
            ),
            "domain": case.domain,
            "confirmed_by": confirmed_by,
            "parity": decision.assessment.parity.to_dict()
            if decision.assessment.parity
            else None,
            "adversary_findings": decision.assessment.findings,
            "refusal_reason": refusal_reason,
            "locked_decisions": list(case.locked_decisions),
            "details": details or {},
        }
        return self._seal_and_append(body_state)

    def record_payment(
        self,
        *,
        invoice_id: str,
        amount: Decimal,
        payment_reference: str,
        assessment: FoundationAssessment,
        authorization: dict[str, Any],
    ) -> dict[str, Any]:
        """Seal the payment initiation into the ledger under the spend decision."""
        body_state = {
            "decision_id": f"spend-{invoice_id}",
            "created_at": _now_iso(),
            "invoice_id": invoice_id,
            "stage": "payment_execution",
            "action": "payment_initiated",
            "decision": "approved",
            "session_status": authorization.get("session_status"),
            "r0_verdict": Verdict.PASS.value,
            "foundation_verdict": Verdict.PASS.value,
            "foundation_score": assessment.score,
            "domain": _SPEND_DOMAIN,
            "confirmed_by": authorization.get("confirmed_by"),
            "parity": assessment.parity.to_dict() if assessment.parity else None,
            "adversary_findings": assessment.findings,
            "refusal_reason": None,
            "locked_decisions": authorization.get("locked_decisions", []),
            "details": {
                "payment_reference": payment_reference,
                "amount": str(amount),
            },
        }
        return self._seal_and_append(body_state)

    def record_refusal(
        self,
        *,
        invoice_id: str,
        stage: str,
        reason: str,
        evaluation: GateEvaluation | None = None,
        assessment: FoundationAssessment | None = None,
    ) -> dict[str, Any]:
        """Record a refusal (R0 halt, parity mismatch, or hold) in the ledger."""
        body_state = {
            "decision_id": f"spend-{invoice_id}",
            "created_at": _now_iso(),
            "invoice_id": invoice_id,
            "stage": stage,
            "action": "refused",
            "decision": "refused",
            "session_status": SessionStatus.HALT.value,
            "r0_verdict": evaluation.verdict.value if evaluation else None,
            "r0_results": dict(evaluation.results) if evaluation else None,
            "foundation_verdict": None,
            "foundation_score": assessment.score if assessment else None,
            "domain": _SPEND_DOMAIN,
            "confirmed_by": None,
            "parity": assessment.parity.to_dict()
            if assessment and assessment.parity
            else None,
            "adversary_findings": assessment.findings if assessment else [],
            "refusal_reason": reason,
            "locked_decisions": [],
            "details": {},
        }
        return self._seal_and_append(body_state)

    # ------------------------------------------------------------------ seal
    def _seal_and_append(self, body_state: dict[str, Any]) -> dict[str, Any]:
        """Serialize the state, seal it in a payload envelope, append the ledger.

        The envelope's ``validate_payload_envelope`` is structure-only, so the
        entry carries its own SHA-256 digest over the exact sealed body.
        """
        body = json.dumps(body_state, sort_keys=True, ensure_ascii=False)
        envelope = build_payload_envelope(body, route=_ENVELOPE_ROUTE)
        entry = {
            **body_state,
            "body": body,
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "envelope": envelope.render(),
        }
        self.records.append(entry)
        return entry
