"""Ivy — Invoice Intake & Extraction Analyst.

auto_discover matches `invoice_intake_agent` to the YAML agent. Ivy (an llm +
super_agent, fanned by `scaling.copies`) reads an invoice through her tools and
produces confidence-scored line-items. The MODEL decides and pulls; this CODE
fails closed.

@chain enforce_intake_floor — the deterministic floor a prompt can't waive:
a field the model scored low, or a line-item missing an amount, can NEVER be
passed downstream as "clean". If anything is uncertain, the extract is forced
un-clean and turned into a question for the AP clerk — a guessed number never
reaches Mat's coding sheet. The floor also RE-ASSERTS the anomaly-risk policy
flags (>=$10K / round-thousand / unresolved vendor / a possible re-submission
of an already-posted invoice): the model may raise them, never lower them. The
last one makes sure every possible duplicate reaches Ray's dedupe floor, whatever
its amount.
"""
from __future__ import annotations

from typing import Any

from leafmesh import LeafMeshLogger, chain

try:
    from ._shared import store
except ImportError:  # pragma: no cover
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _shared import store  # type: ignore

logger = LeafMeshLogger(__name__)

_CONFIDENCE_MIN = 0.85
_ANOMALY_CENTS = 1_000_000       # >= $10,000 is always risk-checked
_ROUND_THOUSAND_CENTS = 100_000  # a round-thousand amount is a classic red flag


def _to_int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _looks_already_posted(result: dict, amount_cents: int) -> bool:
    """Same vendor + same invoice number, or same vendor + amount + due date,
    already in the posted ledger. Ray decides; this only makes sure he sees it."""
    vendor_id = str(result.get("vendor_id") or "").strip()
    if not vendor_id:
        return False
    inv_id = str(result.get("invoice_id") or "").strip()
    due = str(result.get("due_date") or "").strip()
    for row in store.posted_for_vendor(vendor_id):
        if inv_id and row.get("invoice_id") == inv_id:
            return True
        if amount_cents and due and int(row.get("amount_cents") or 0) == amount_cents \
                and row.get("due_date") == due:
            return True
    return False


def enforce_intake_floor(result: Any, context: dict) -> dict:
    """Fail closed: never hand a low-confidence / incomplete extract downstream."""
    result = result if isinstance(result, dict) else {}
    line_items = result.get("line_items") or []
    low_conf = list(result.get("low_confidence_fields") or [])

    # Deterministic re-check the model cannot talk its way past.
    for li in line_items if isinstance(line_items, list) else []:
        if not isinstance(li, dict):
            continue
        if li.get("amount") in (None, "", 0) and not li.get("amount_zero_ok"):
            low_conf.append(f"{li.get('line_id', 'line')}:amount")
        for field in ("description", "amount", "quantity"):
            score = li.get(f"{field}_confidence")
            if isinstance(score, (int, float)) and score < _CONFIDENCE_MIN:
                low_conf.append(f"{li.get('line_id', 'line')}:{field}")

    low_conf = sorted(set(low_conf))
    result["low_confidence_fields"] = low_conf

    # Re-assert the anomaly-risk policy floor (model may RAISE, never lower).
    amount_cents = _to_int(result.get("amount_cents"))
    anomaly = bool(result.get("anomaly_risk"))
    if amount_cents >= _ANOMALY_CENTS:
        anomaly = True
    if amount_cents > 0 and amount_cents % _ROUND_THOUSAND_CENTS == 0:
        anomaly = True
    if not str(result.get("vendor_id") or "").strip():
        anomaly = True
    if _looks_already_posted(result, amount_cents):
        anomaly = True   # possible re-submission -> Ray's dedupe floor decides
    result["anomaly_risk"] = anomaly

    if low_conf:
        result["extract_clean"] = False
        if not result.get("question_for_reviewer"):
            result["question_for_reviewer"] = (
                "We could not read these with confidence — please confirm: "
                + ", ".join(low_conf)
            )
        logger.info(f"[ivy] confidence floor held extract un-clean: {low_conf}")
    else:
        # Only 'clean' when there is genuinely something and nothing uncertain.
        result["extract_clean"] = bool(line_items) and not result.get("policy_flags")
    return result


@chain(enforce_intake_floor)
async def invoice_intake_agent(llm_response: Any, input_data: dict, context: dict) -> dict:
    """Return the model's confidence-scored extraction; the floor re-checks it."""
    out = llm_response if isinstance(llm_response, dict) else {}
    # Normalise to the yields contract so downstream conditions never read a
    # missing field (conditions fail closed on absence).
    out.setdefault("extract_clean", False)
    out.setdefault("doc_class", "ap_invoice")
    out.setdefault("low_confidence_fields", [])
    out.setdefault("policy_flags", [])
    out.setdefault("line_items", [])
    out.setdefault("invoice_id", "")
    out.setdefault("vendor_id", "")
    out.setdefault("amount_cents", 0)
    out.setdefault("po_number", "")
    out.setdefault("due_date", "")
    out.setdefault("anomaly_risk", False)
    out.setdefault("question_for_reviewer", "")
    return out
