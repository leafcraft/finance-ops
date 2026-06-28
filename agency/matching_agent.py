"""matching_agent — PO match + 3-way match, store-grounded.

KEPT AS PYTHON (class: ERP lookup + domain math + loop guard). Three
patterns, each serving a real domain need:

@pre_compose  load_po_context — pull the PURCHASE ORDER (+ its goods
              receipt) and the vendor-master record from the source
              store BEFORE matching runs. In dev these come from
              agency/_shared/store.py (seeded); in prod from the ERP
              connector. This is the self-reliant context pull: matching
              fetches its own authority rather than trusting whatever
              the upstream LLM happened to echo.
@chain        enforce_match_floor — the deterministic 3-way TOLERANCE
              floor (agency/_shared/matching_math). Quantity / unit-price
              variance against the PO + goods receipt is checked against
              fixed tolerances; any line outside tolerance is a gap and
              FAILS the match. The LLM (or a fat-fingered direct call)
              cannot present an out-of-tolerance invoice as a clean match.

Bidirectional: on PO miss it calls invoice_intake_agent back for ONE
re-extraction pass with PO context (bounded by the session stash — the
SDK chain cap would otherwise kill the session). On exhaustion,
po_retry_exhausted routes to vendor_comms instead of looping.

Full-shape returns on every path; the 3-way fields default vacuously
when the 3-way phase is skipped.
"""
from __future__ import annotations

from typing import Any

from leafmesh import LeafMeshLogger, chain, pre_compose

from agency._shared import store
from agency._shared.matching_math import (
    PO_MATCH_FLOOR, po_line_match, three_way_match,
)
from agency._shared.session_stash import resolve_session_id, stash_get, stash_put

logger = LeafMeshLogger(__name__)

_MAX_REEXTRACTION_ATTEMPTS = 1


def _up(input_data: dict[str, Any], key: str, default: Any = None) -> Any:
    """Upstream read: flat input_data, then flat upstream_yields."""
    if input_data.get(key) not in (None, ""):
        return input_data[key]
    uy = input_data.get("upstream_yields")
    if isinstance(uy, dict) and uy.get(key) not in (None, ""):
        return uy[key]
    return default


# ── @pre_compose — pull the PO + receipt + vendor record (self-reliant) ──
def load_po_context(data: Any, ctx: Any) -> dict[str, Any]:
    """Fetch the matching authority from the source store. PROD: ERP read."""
    input_data = data if isinstance(data, dict) else {}
    po_number = str(_up(input_data, "po_number", "") or "")
    vendor_id = str(_up(input_data, "vendor_id", "") or "")
    po = store.get_po(po_number)
    vendor = store.get_vendor(vendor_id)
    return {
        "po": po,
        "po_present": bool(po),
        "vendor": vendor,
        "vendor_requires_3way": bool(vendor.get("requires_3way")) if vendor else False,
    }


def _po_ctx(context: Any) -> dict[str, Any]:
    prepared = (context or {}).get("prepared_data", {}) if isinstance(context, dict) else {}
    c = prepared.get("context", {}) if isinstance(prepared, dict) else {}
    return c if isinstance(c, dict) else {}


# ── @chain — the deterministic 3-way TOLERANCE floor (LLM can't waive) ──
async def enforce_match_floor(result: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Re-assert the tolerance arithmetic over whatever the body produced.

    Even on a direct call that hands in a flattering match_percentage, the
    floor recomputes from the PO + goods receipt and fails any line
    outside tolerance. A clean 3-way is impossible without the receipt
    reconciling within tolerance.
    """
    if not isinstance(result, dict):
        result = {}
    # A 3-way result is only allowed to read 'matched' if there were no
    # gaps AND a receipt was found — the body sets gaps from the math,
    # so OR-in the rule (the LLM/body may not LOWER a gap into a pass).
    if result.get("three_way_matched") is True:
        if result.get("gaps") or not result.get("receipt_found"):
            result["three_way_matched"] = False
            logger.warning(
                "[matching] 3-way floor OVERRODE a 'matched' verdict — "
                f"gaps={result.get('gaps')} receipt_found={result.get('receipt_found')}"
            )
    # PO match cannot clear the floor below PO_MATCH_FLOOR.
    if result.get("po_found") and float(result.get("match_percentage") or 0) < PO_MATCH_FLOOR:
        result.setdefault("discrepancies", [])
        if "below_po_match_floor" not in result["discrepancies"]:
            result["discrepancies"].append("below_po_match_floor")
    return result


@pre_compose(context_processor=load_po_context)
@chain(enforce_match_floor)
async def matching_agent(
    llm_response: dict[str, Any] | str | None,   # programmatic — unused
    input_data: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Look up the PO, match lines, then 3-way match when required."""
    input_data = input_data if isinstance(input_data, dict) else {}
    pctx = _po_ctx(context)
    po = pctx.get("po") or store.get_po(str(_up(input_data, "po_number", "") or ""))
    po_number = str(_up(input_data, "po_number", "") or "")
    line_items = list(_up(input_data, "line_items", []) or [])
    invoice_id = str(_up(input_data, "invoice_id", "") or "")

    # ── Phase 1: PO match (store-grounded tolerance arithmetic) ──────
    result = po_line_match(po, line_items)
    if not result["po_id"]:
        result["po_id"] = po_number

    # ── Phase 2: 3-way match, when required by amount/policy or vendor ─
    requires_3way = bool(_up(input_data, "requires_3way_match", False)) \
        or bool(pctx.get("vendor_requires_3way"))
    if requires_3way:
        result.update(three_way_match(po, line_items))
    else:
        result.update({
            "three_way_matched": False, "receipt_found": False,
            "quantity_variance": 0, "price_variance_pct": 0.0, "gaps": [],
        })

    # ── Loop guard: bound the re-extraction callback ─────────────────
    result["po_retry_exhausted"] = False
    if not result.get("po_found"):
        session_id = resolve_session_id(input_data, context)
        stash = stash_get(session_id)
        attempts = int(stash.get("po_reextraction_attempts") or 0)
        if attempts >= _MAX_REEXTRACTION_ATTEMPTS:
            result["po_retry_exhausted"] = True
            logger.warning(
                f"[matching] {invoice_id or '?'}: PO {po_number or '(none)'} "
                f"still not found after {attempts} re-extraction(s) — "
                f"routing to vendor_comms instead of looping"
            )
        else:
            stash["po_reextraction_attempts"] = attempts + 1
            stash_put(session_id, stash)

    logger.info(
        f"[matching] {invoice_id or '?'} po_found={result['po_found']} "
        f"match={result['match_percentage']} 3way={result['three_way_matched']} "
        f"gaps={result.get('gaps')}"
    )
    return result
