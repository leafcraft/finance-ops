"""Ray — Payment-Risk Officer.

auto_discover matches `payment_risk_agent`. Ray (an llm) writes the fraud
narrative; this CODE owns the duplicate floor the model cannot waive.

@chain enforce_dup_floor — SHA-256 dedupe (agency/_shared/dedupe) against the
vendor's POSTED ledger (agency/_shared/store — the ERP AP ledger in prod). A hit
FORCES is_duplicate + fraud_score >= 0.85 + recommendation=controller_review; a
confirmed duplicate is never presented as clean. The floor also clamps the
cardinal rule (no fraud_score >= 0.7 on a single signal) and derives the
recommendation from the final score so the outbound wires route correctly.
"""
from __future__ import annotations

from typing import Any

from leafmesh import LeafMeshLogger, chain

try:
    from ._shared import dedupe, store
except ImportError:  # pragma: no cover
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _shared import dedupe, store  # type: ignore

logger = LeafMeshLogger(__name__)

_DUP_SCORE_FLOOR = 0.85
_SINGLE_SIGNAL_CAP = 0.55


def _invoice_from(input_data: dict) -> dict[str, Any]:
    """Assemble the invoice identity from the handoff (flat or upstream_yields)."""
    up = input_data.get("upstream_yields")
    src = up if isinstance(up, dict) and up.get("vendor_id") else input_data
    return {
        "invoice_id": str(src.get("invoice_id") or ""),
        "vendor_id": str(src.get("vendor_id") or ""),
        "amount_cents": src.get("amount_cents") or 0,
        "due_date": str(src.get("due_date") or ""),
    }


def _to_float(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def enforce_dup_floor(result: Any, context: dict) -> dict:
    """Fail closed: a confirmed duplicate can never pass as clean or low-risk."""
    result = result if isinstance(result, dict) else {}
    invoice = _invoice_from(result.get("_input", {}))

    prior = store.posted_for_vendor(invoice["vendor_id"])
    match = dedupe.find_duplicate(invoice, prior, detection_mode="loose")

    score = _to_float(result.get("fraud_score"))
    signals = list(result.get("flagged_signals") or [])

    if match.get("is_duplicate"):
        result["is_duplicate"] = True
        result["duplicate_of_invoice_id"] = match.get("duplicate_of_invoice_id") or ""
        score = max(score, _DUP_SCORE_FLOOR)
        kind = match.get("duplicate_match_kind", "duplicate")
        if kind not in signals:
            signals.append(kind)
        if not result.get("briefing"):
            result["briefing"] = (
                f"Confirmed duplicate ({kind}) of invoice "
                f"{result['duplicate_of_invoice_id'] or 'on record'} — HOLD pending "
                f"vendor confirmation; do not pay twice."
            )
    else:
        result.setdefault("is_duplicate", False)
        result.setdefault("duplicate_of_invoice_id", "")
        # Single-signal cap — no high score on one weak signal (duplicate excepted).
        if score >= 0.7 and len(signals) < 2:
            score = min(score, _SINGLE_SIGNAL_CAP)

    result["fraud_score"] = round(min(max(score, 0.0), 1.0), 4)
    result["flagged_signals"] = signals

    # Derive the recommendation from the FINAL score so the wires route right.
    s = result["fraud_score"]
    if result["is_duplicate"] or s >= 0.7:
        result["recommendation"] = "controller_review"
    elif s >= 0.4:
        result["recommendation"] = "flag"
    else:
        result["recommendation"] = "pass"
    result.setdefault("briefing", "")
    logger.info(f"[ray] dup={result['is_duplicate']} score={result['fraud_score']} "
                f"rec={result['recommendation']}")
    return result


@chain(enforce_dup_floor)
async def payment_risk_agent(llm_response: Any, input_data: dict, context: dict) -> dict:
    out = llm_response if isinstance(llm_response, dict) else {}
    out.setdefault("fraud_score", 0)
    out.setdefault("flagged_signals", [])
    out.setdefault("is_duplicate", False)
    out.setdefault("duplicate_of_invoice_id", "")
    out.setdefault("recommendation", "pass")
    out.setdefault("briefing", "")
    # Carry the handoff so the floor can read the invoice identity.
    out["_input"] = input_data if isinstance(input_data, dict) else {}
    return out
