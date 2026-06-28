"""loan_origination_agent — for internal-credit / vendor-financing / customer-loans.

KEPT AS PYTHON (class: domain math) — the deterministic underwriting
matrix (FICO / DTI / DSCR / corporate-rating gates + amount-tiered
approval) is credit policy that must never drift with a model.

Realises Lyzr's banking blueprint for finance teams that extend
credit (B2B customer financing, vendor financing, equipment leasing).
Reads application → pulls credit signal → applies policy → produces
a decision with HITL above auto-approve threshold.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from leafmesh import LeafMeshLogger

logger = LeafMeshLogger(__name__)

_DB = Path("./logs/loan_origination.db")

# Auto-approve thresholds (in cents) and policy bands
AUTO_APPROVE_CENTS = 5_000_000      # $50K
MANAGER_APPROVE_CENTS = 25_000_000  # $250K
CFO_APPROVE_CENTS = 100_000_000     # $1M+

# Minimum credit signals
MIN_DSCR = 1.20             # Debt Service Coverage Ratio
MAX_DTI = 0.45              # Debt-to-Income
MIN_FICO = 680              # for individual applicants
MIN_CORP_RATING = "BB"      # for corporate applicants


def _up(input_data: dict[str, Any], context: dict[str, Any],
        key: str, default: Any = None) -> Any:
    """Upstream read: flat input_data, then flat upstream_yields."""
    if input_data.get(key) not in (None, ""):
        return input_data[key]
    uy = input_data.get("upstream_yields")
    if isinstance(uy, dict) and uy.get(key) not in (None, ""):
        return uy[key]
    return default


def _ensure_db():
    _DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS loan_applications (
            application_id TEXT PRIMARY KEY,
            applicant_id TEXT,
            applicant_type TEXT,           -- individual | corporate
            amount_cents INTEGER,
            term_months INTEGER,
            purpose TEXT,
            decision TEXT,                 -- approved | declined | manual_review
            decision_rationale TEXT,
            approval_tier TEXT,            -- auto | manager | cfo | board
            credit_signals TEXT,           -- JSON
            decided_at_utc TEXT
        )
    """)
    return conn


def _corp_rating_score(rating: str) -> int:
    """S&P-style rating → numeric score. Higher = better."""
    order = ["D", "C", "CC", "CCC", "B-", "B", "B+",
             "BB-", "BB", "BB+", "BBB-", "BBB", "BBB+",
             "A-", "A", "A+", "AA-", "AA", "AA+", "AAA"]
    try:
        return order.index(rating.upper())
    except ValueError:
        return 0


async def loan_origination_agent(llm_response, input_data, context):
    if isinstance(llm_response, str):
        try:
            llm_response = json.loads(llm_response)
        except (ValueError, TypeError):
            llm_response = {}
    if not isinstance(llm_response, dict):
        llm_response = {}

    application_id = str(_up(input_data, context, "application_id", "") or "")
    if not application_id:
        # Full yields shape — a malformed application must still
        # escalate (manual_review at manager tier), not vanish.
        return {
            "application_id": "", "applicant_id": "",
            "applicant_type": "", "amount_cents": 0, "term_months": 0,
            "decision": "manual_review", "approval_tier": "manager",
            "decision_rationale": "missing_application_id",
            "decline_reasons": ["missing_application_id"],
            "credit_signals": {},
            "requires_manual_approval": True,
            "decided_at_utc": datetime.now(timezone.utc).isoformat(),
        }

    applicant_id = str(_up(input_data, context, "applicant_id", "") or "")
    applicant_type = str(_up(input_data, context, "applicant_type", "")
                          or "individual").lower()
    amount = int(_up(input_data, context, "amount_cents", 0) or 0)
    term_months = int(_up(input_data, context, "term_months", 0) or 12)
    purpose = str(_up(input_data, context, "purpose", "") or "")

    # Credit signals from upstream KYC + credit pull
    fico = int(llm_response.get("fico_score") or 0)
    dscr = float(llm_response.get("dscr") or 0.0)
    dti = float(llm_response.get("dti") or 0.0)
    corp_rating = str(llm_response.get("corporate_rating") or "")
    kyc_status = str(_up(input_data, context, "kyc_status", "")
                      or llm_response.get("kyc_status") or "pending").lower()

    # Hard policy gates
    decision = "approved"
    decline_reasons: list[str] = []

    if kyc_status != "approved":
        decision = "declined"
        decline_reasons.append(f"kyc_status_{kyc_status}")

    if applicant_type == "individual":
        if fico < MIN_FICO:
            decision = "declined"; decline_reasons.append(f"fico_below_{MIN_FICO}")
        if dti > MAX_DTI:
            decision = "declined"; decline_reasons.append(f"dti_above_{MAX_DTI}")
    elif applicant_type == "corporate":
        if dscr < MIN_DSCR:
            decision = "declined"; decline_reasons.append(f"dscr_below_{MIN_DSCR}")
        if _corp_rating_score(corp_rating) < _corp_rating_score(MIN_CORP_RATING):
            decision = "declined"; decline_reasons.append(f"rating_below_{MIN_CORP_RATING}")

    # Approval tier based on amount
    if decision == "approved":
        if amount <= AUTO_APPROVE_CENTS:
            approval_tier = "auto"
        elif amount <= MANAGER_APPROVE_CENTS:
            approval_tier = "manager"
            decision = "manual_review"
        elif amount <= CFO_APPROVE_CENTS:
            approval_tier = "cfo"
            decision = "manual_review"
        else:
            approval_tier = "board"
            decision = "manual_review"
    else:
        approval_tier = "n/a"

    rationale = (
        " | ".join(decline_reasons) if decline_reasons
        else f"Auto-approved within ${AUTO_APPROVE_CENTS/100:.0f} threshold"
        if approval_tier == "auto"
        else f"Manual review required at {approval_tier} tier"
    )

    now = datetime.now(timezone.utc).isoformat()
    conn = _ensure_db()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO loan_applications
                (application_id, applicant_id, applicant_type,
                 amount_cents, term_months, purpose, decision,
                 decision_rationale, approval_tier, credit_signals,
                 decided_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (application_id, applicant_id, applicant_type, amount,
              term_months, purpose, decision, rationale, approval_tier,
              json.dumps({"fico": fico, "dscr": dscr, "dti": dti,
                          "corp_rating": corp_rating,
                          "kyc_status": kyc_status}),
              now))
        conn.commit()
    finally:
        conn.close()

    logger.info(
        f"[loan-orig] {application_id} ${amount/100:.0f} decision={decision} "
        f"tier={approval_tier} reasons={decline_reasons or 'clean'}"
    )

    return {
        "application_id": application_id,
        "applicant_id": applicant_id,
        "applicant_type": applicant_type,
        "amount_cents": amount,
        "term_months": term_months,
        "decision": decision,
        "approval_tier": approval_tier,
        "decision_rationale": rationale,
        "decline_reasons": decline_reasons,
        "credit_signals": {
            "fico": fico, "dscr": dscr, "dti": dti,
            "corp_rating": corp_rating, "kyc_status": kyc_status,
        },
        "requires_manual_approval": decision == "manual_review",
        "decided_at_utc": now,
    }
