"""payment_risk_agent — duplicate pre-flight + fraud verdict.

The LLM (configs/config.yaml) writes the fraud narrative + signal cites;
this micro-module owns the two things the LLM must NOT be trusted to do:

@pre_compose  load_dup_candidates — pull the vendor's PRIOR POSTED
              invoices from the source store (the AP ledger in prod) so
              the duplicate check has real evidence, not a hallucinated
              "I checked my knowledge base". Self-reliant context pull.
@chain        enforce_dup_floor — the deterministic DUPLICATE-HASHING
              floor (agency/_shared/dedupe). strict = vendor+invoice_id;
              loose = vendor+amount+due_date. A hash hit FORCES
              is_duplicate=true, fraud_score >= 0.85, and
              recommendation=controller_review — the LLM cannot score a
              confirmed duplicate as a pass. It also clamps the cardinal
              fraud rule (no >= 0.7 on a single signal) the prompt states.
@compose      per-downstream payloads — the three callees need different
              things: the Controller gets the briefing + signals to
              defend the decision in an audit; vendor_comms gets the
              duplicate/gap context to draft the vendor query; gl_coding
              gets only the clean-pass identity passthrough.

Everything else (the narrative, signal selection, controller briefing)
stays prompt-driven and SDK yields-filled.
"""
from __future__ import annotations

import json
from typing import Any

from leafmesh import LeafMeshLogger, chain, compose, pre_compose

from agency._shared import store
from agency._shared.dedupe import find_duplicate

logger = LeafMeshLogger(__name__)

_DUP_FRAUD_FLOOR = 0.85
_SINGLE_SIGNAL_CAP = 0.55


def _up(input_data: dict[str, Any], key: str, default: Any = None) -> Any:
    if input_data.get(key) not in (None, ""):
        return input_data[key]
    uy = input_data.get("upstream_yields")
    if isinstance(uy, dict) and uy.get(key) not in (None, ""):
        return uy[key]
    return default


def _invoice_from(input_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "invoice_id": str(_up(input_data, "invoice_id", "") or ""),
        "vendor_id": str(_up(input_data, "vendor_id", "") or ""),
        "amount_cents": int(_up(input_data, "amount_cents", 0) or 0),
        "due_date": str(_up(input_data, "due_date", "") or ""),
    }


# ── @pre_compose — pull the vendor's posted-invoice history ───────────
def load_dup_candidates(data: Any, ctx: Any) -> dict[str, Any]:
    """Fetch prior posted invoices for this vendor. PROD: ERP AP ledger."""
    input_data = data if isinstance(data, dict) else {}
    inv = _invoice_from(input_data)
    prior = store.posted_for_vendor(inv["vendor_id"])
    vendor = store.get_vendor(inv["vendor_id"])
    return {
        "invoice": inv,
        "prior_posted": prior,
        "prior_posted_count": len(prior),
        "vendor_median_cents": int(vendor.get("historical_median_cents") or 0) if vendor else 0,
        "detection_mode": str(_up(input_data, "detection_mode", "") or ""),
    }


def _ctx(context: Any) -> dict[str, Any]:
    prepared = (context or {}).get("prepared_data", {}) if isinstance(context, dict) else {}
    c = prepared.get("context", {}) if isinstance(prepared, dict) else {}
    return c if isinstance(c, dict) else {}


def _parse(resp: Any) -> dict[str, Any]:
    if isinstance(resp, dict):
        return resp
    if isinstance(resp, str):
        try:
            return json.loads(resp)
        except (ValueError, TypeError):
            return {}
    return {}


# ── @chain — the deterministic duplicate-hashing floor ───────────────
async def enforce_dup_floor(result: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Run the dedupe hash over the vendor's posted history and force the
    duplicate verdict. The LLM may RAISE risk but never LOWER a confirmed
    duplicate to a pass.

    The agent body attaches the resolved invoice + prior posted history
    as private `_dup_*` keys (it has input_data); this floor consumes
    and strips them, so it fires identically through the SDK runtime and
    on a direct call.
    """
    if not isinstance(result, dict):
        result = {}
    inv = result.pop("_dup_invoice", None) or {}
    prior = result.pop("_dup_prior", None)
    requested_mode = str(result.pop("_dup_requested_mode", "") or "").strip().lower()
    if prior is None:
        prior = store.posted_for_vendor(inv.get("vendor_id", ""))
    # standalone duplicate_check entry sets detection_mode → preflight
    mode = requested_mode or str(result.get("detection_mode") or "strict")

    match = find_duplicate(inv, prior, detection_mode=mode)

    # Floor: a hash hit forces the duplicate verdict regardless of the LLM.
    if match["is_duplicate"]:
        result["is_duplicate"] = True
        result["duplicate_match_kind"] = match["duplicate_match_kind"]
        result["duplicate_of_invoice_id"] = match["duplicate_of_invoice_id"]
        result["prior_post_record_id"] = match["prior_post_record_id"]
        result["prior_post_at_utc"] = match["prior_post_at_utc"]
        result["fraud_score"] = max(float(result.get("fraud_score") or 0), _DUP_FRAUD_FLOOR)
        result["recommendation"] = "controller_review"
        if not result.get("duplicate_briefing"):
            result["duplicate_briefing"] = (
                f"HOLD: invoice {inv.get('invoice_id')} matches prior posted "
                f"{match['duplicate_of_invoice_id']} "
                f"({match['duplicate_match_kind']}). Confirm with vendor before pay."
            )
        logger.warning(
            f"[payment-risk] DUPLICATE floor fired ({match['duplicate_match_kind']}) "
            f"invoice={inv.get('invoice_id')} → fraud_score forced >= {_DUP_FRAUD_FLOOR}"
        )
    else:
        result.setdefault("is_duplicate", False)

    result["detection_mode"] = mode
    # risk_check_mode: preflight when the standalone duplicate_check entry
    # passed a detection_mode; inline otherwise (called off intake).
    result["risk_check_mode"] = "preflight" if requested_mode else \
        (result.get("risk_check_mode") or "inline")

    # Cardinal fraud rule clamp: never >= 0.7 on a single cited signal
    # (the sole exception is a confirmed duplicate, handled above).
    try:
        score = float(result.get("fraud_score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    signals = result.get("flagged_signals")
    n_signals = len(signals) if isinstance(signals, list) else 0
    if not result.get("is_duplicate") and score >= 0.7 and n_signals < 2:
        result["fraud_score"] = _SINGLE_SIGNAL_CAP
        score = _SINGLE_SIGNAL_CAP
        logger.info("[payment-risk] single-signal clamp → fraud_score capped at 0.55")

    # Recommendation derived deterministically from the (clamped) score
    # unless a duplicate already forced controller_review.
    if not result.get("is_duplicate"):
        result["recommendation"] = (
            "pass" if score < 0.4 else
            "flag" if score < 0.7 else "controller_review"
        )
    return result


# ── @compose — per-downstream payloads (each callee needs different data) ─
def _to_controller(result: dict, ctx: Any) -> dict[str, Any]:
    """The Controller defends the decision in an audit — give the briefing,
    the signals, and the dollar/identity facts."""
    return {
        "invoice_id": result.get("duplicate_of_invoice_id") or "",
        "vendor_name": "",
        "amount_cents": 0,
        "flagged_signals": result.get("flagged_signals", []),
        "gl_account": "",
        "controller_briefing": result.get("controller_briefing", "")
            or result.get("duplicate_briefing", ""),
        "fraud_score": result.get("fraud_score", 0),
        "is_duplicate": result.get("is_duplicate", False),
    }


def _to_vendor_comms(result: dict, ctx: Any) -> dict[str, Any]:
    """vendor_comms drafts the vendor query — give it the gap/dup context."""
    return {
        "comms_mode": "exception_query",
        "gap_context": {
            "reason": "duplicate" if result.get("is_duplicate") else "fraud_flag",
            "fraud_score": result.get("fraud_score", 0),
            "flagged_signals": result.get("flagged_signals", []),
            "duplicate_of_invoice_id": result.get("duplicate_of_invoice_id", ""),
            "duplicate_match_kind": result.get("duplicate_match_kind", ""),
        },
    }


def _to_gl_coding(result: dict, ctx: Any) -> dict[str, Any]:
    """A clean inline pass — gl_coding only needs the risk verdict echo;
    identity comes from the fan-in of the upstream chain."""
    return {
        "fraud_score": result.get("fraud_score", 0),
        "recommendation": result.get("recommendation", ""),
        "risk_check_mode": result.get("risk_check_mode", ""),
    }


@pre_compose(context_processor=load_dup_candidates)
@compose(
    controller_human=_to_controller,
    vendor_comms_agent=_to_vendor_comms,
    gl_coding_agent=_to_gl_coding,
)
@chain(enforce_dup_floor)
async def payment_risk_agent(
    llm_response: dict[str, Any] | str | None,
    input_data: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Surface the LLM's fraud verdict; the chain enforces the dup floor."""
    input_data = input_data if isinstance(input_data, dict) else {}
    parsed = _parse(llm_response)
    # Resolve the invoice + prior history here (we have input_data) and
    # hand them to enforce_dup_floor via private carry keys. Prefer the
    # @pre_compose context when present, else read input/store directly.
    pctx = _ctx(context)
    inv = pctx.get("invoice") or _invoice_from(input_data)
    prior = pctx.get("prior_posted")
    if prior is None:
        prior = store.posted_for_vendor(inv.get("vendor_id", ""))
    requested_mode = pctx.get("detection_mode") or _up(input_data, "detection_mode", "")

    result: dict[str, Any] = {
        "fraud_score": float(parsed.get("fraud_score") or 0),
        "flagged_signals": parsed.get("flagged_signals") or [],
        "duplicate_of_invoice_id": str(parsed.get("duplicate_of_invoice_id") or ""),
        "recommendation": str(parsed.get("recommendation") or ""),
        "controller_briefing": str(parsed.get("controller_briefing") or ""),
        "is_duplicate": bool(parsed.get("is_duplicate") or False),
        "duplicate_match_kind": str(parsed.get("duplicate_match_kind") or ""),
        "prior_post_record_id": str(parsed.get("prior_post_record_id") or ""),
        "prior_post_at_utc": str(parsed.get("prior_post_at_utc") or ""),
        "detection_mode": str(parsed.get("detection_mode") or ""),
        "duplicate_briefing": str(parsed.get("duplicate_briefing") or ""),
        "risk_check_mode": str(parsed.get("risk_check_mode") or ""),
        # Private carry → consumed + stripped by enforce_dup_floor.
        "_dup_invoice": inv,
        "_dup_prior": prior,
        "_dup_requested_mode": str(requested_mode or ""),
    }
    return result
