"""Mat — Matching & GL-Coding Builder.

auto_discover matches `matching_gl_agent`. Mat turns Ivy's verified line-items
into a coded entry and gets it to the right approval tier. Deliberately SEPARATE
from Ivy (reader != coder).

@chain enforce_cross_check_floor — the deterministic floor:
  * PO / 3-way match arithmetic (agency/_shared/matching_math) against the ERP
    PO + goods receipt (store-backed in dev) — a line outside tolerance fails.
  * GL-validity: an account not ACTIVE in the chart forces confidence < 0.7
    (which routes the entry to the Controller, never the AP clerk).
  * totals must tie out to the source; if they don't, return to Ivy with the
    specific discrepancy — a BOUNDED back-edge (match_retry_exhausted counts the
    round in the session stash) — NEVER adjust a number to make it balance.
A clean, tied-out entry renders the coding sheet to ./out and is stashed for the
finisher to recover after the human approval step.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from leafmesh import LeafMeshLogger, chain

try:
    from ._shared import matching_math, store
    from ._shared.session_stash import resolve_session_id, stash_get, stash_put
except ImportError:  # pragma: no cover
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _shared import matching_math, store  # type: ignore
    from _shared.session_stash import resolve_session_id, stash_get, stash_put  # type: ignore

logger = LeafMeshLogger(__name__)

_OUT_DIR = Path("./out")
_MAX_MATCH_RETRIES = 1            # one re-extraction pass, then terminate to the Lead
_THREE_WAY_CENTS = 500_000       # >= $5,000 requires a 3-way match
_TOTAL_TOLERANCE_CENTS = 100     # $1.00 rounding tolerance on the tie-out
_GL_CONF_FLOOR = 0.7
_FALLBACK_GL = "6999-Uncategorized"


def _to_int(v: Any) -> int:
    try:
        return int(round(float(v or 0)))
    except (TypeError, ValueError):
        return 0


def _recover_invoice(input_data: dict, context: dict) -> dict[str, Any]:
    """Recover the invoice payload across the direct OR risk-detour path."""
    up = input_data.get("upstream_yields")
    carry = input_data.get("_input")
    for src in (input_data,
                up if isinstance(up, dict) else None,
                carry if isinstance(carry, dict) else None):
        if isinstance(src, dict) and src.get("line_items"):
            return src
    sid = resolve_session_id(input_data, context)
    stashed = stash_get(sid)
    if stashed.get("line_items"):
        return stashed
    inv_id = str(input_data.get("invoice_id") or "")
    return store.find_invoice(inv_id) or {"invoice_id": inv_id, "line_items": []}


def _line_total_cents(li: dict) -> int:
    if not isinstance(li, dict):
        return 0
    if li.get("amount_cents") is not None:
        return _to_int(li["amount_cents"])
    if li.get("total") is not None:
        return _to_int(float(li["total"]) * 100)
    qty = _to_int(li.get("quantity"))
    up = li.get("unit_price")
    if up is not None:
        return _to_int(float(up) * 100) * (qty or 1)
    return 0


def _render_sheet(inv: dict, gl_account: str, cost_center: str) -> str:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    inv_id = str(inv.get("invoice_id") or "invoice")
    path = _OUT_DIR / f"{inv_id}_coding_sheet.md"
    lines = [f"# GL coding sheet — {inv_id}",
             f"Vendor: {inv.get('vendor_id','')}    PO: {inv.get('po_number','')}",
             f"GL account: {gl_account}    Cost center: {cost_center}", ""]
    total = 0
    for li in inv.get("line_items") or []:
        c = _line_total_cents(li)
        total += c
        lines.append(f"- {li.get('description','')}  qty {li.get('quantity','')}  "
                     f"${c/100:,.2f}")
    lines += ["", f"**Total: ${total/100:,.2f}**", "",
              "_Draft coding sheet — awaiting approval. Every line traced to the source._"]
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def enforce_cross_check_floor(result: Any, context: dict) -> dict:
    """Fail closed: match, code, and tie out — or send it back to Ivy (bounded)."""
    result = result if isinstance(result, dict) else {}
    inv = result.get("_invoice") or {}
    input_data = result.get("_input") or {}
    sid = resolve_session_id(input_data, context)

    line_items = inv.get("line_items") or []
    amount_cents = _to_int(inv.get("amount_cents")) or sum(
        _line_total_cents(li) for li in line_items if isinstance(li, dict))
    result["invoice_id"] = str(inv.get("invoice_id") or "")
    result["vendor_id"] = str(inv.get("vendor_id") or "")
    result["amount_cents"] = amount_cents
    result["anomaly_risk"] = bool(inv.get("anomaly_risk") or input_data.get("anomaly_risk"))

    # ── PO / 3-way match (deterministic arithmetic) ──────────────────────────
    vendor = store.get_vendor(result["vendor_id"])
    po = store.get_po(str(inv.get("po_number") or ""))
    three_way = bool(vendor.get("requires_3way")) or amount_cents >= _THREE_WAY_CENTS
    discrepancy = ""
    if inv.get("po_number"):
        pm = matching_math.po_line_match(po, line_items)
        match_ok = pm["po_found"] and pm["match_percentage"] >= matching_math.PO_MATCH_FLOOR
        if not match_ok:
            discrepancy = f"PO match failed: {pm.get('discrepancies')}"
        if match_ok and three_way:
            tw = matching_math.three_way_match(po, line_items)
            if not tw["three_way_matched"]:
                match_ok = False
                discrepancy = f"3-way match failed: {tw.get('gaps')}"
    else:
        # No PO cited — allowed only when the vendor does not require one.
        match_ok = not vendor.get("requires_po", False)
        if not match_ok:
            discrepancy = "vendor requires a PO but none was cited"
    result["match_ok"] = match_ok

    # ── GL validity (an inactive/unknown account forces low confidence) ───────
    gl_account = str(result.get("gl_account") or "").strip() or vendor.get("default_gl_account", "")
    confidence = result.get("confidence")
    confidence = float(confidence) if isinstance(confidence, (int, float)) else 0.8
    if not store.gl_account_exists(gl_account):
        gl_account = vendor.get("default_gl_account") if store.gl_account_exists(
            vendor.get("default_gl_account", "")) else _FALLBACK_GL
        confidence = min(confidence, _GL_CONF_FLOOR - 0.05)
    result["gl_account"] = gl_account
    result["cost_center"] = str(result.get("cost_center") or vendor.get("default_cost_center") or "CC-OPEX")
    result["confidence"] = round(max(0.0, min(confidence, 1.0)), 4)

    # ── Tie-out ──────────────────────────────────────────────────────────────
    computed = sum(_line_total_cents(li) for li in line_items if isinstance(li, dict))
    ties = bool(line_items) and abs(computed - amount_cents) <= _TOTAL_TOLERANCE_CENTS
    if not ties and not discrepancy:
        discrepancy = f"line-items sum ${computed/100:,.2f} != stated ${amount_cents/100:,.2f}"

    cross_ok = match_ok and ties
    result["cross_check_ok"] = cross_ok
    result["discrepancy"] = "" if cross_ok else discrepancy

    if not cross_ok:
        # Bounded back-edge to Ivy — count the round; terminate on exhaustion.
        stashed = stash_get(sid)
        rounds = int(stashed.get("_match_retries", 0)) + 1
        stashed["_match_retries"] = rounds
        stash_put(sid, stashed)
        result["match_retry_exhausted"] = rounds > _MAX_MATCH_RETRIES
        result["artifact_path"] = ""
        # Delete any stale draft so a blocked entry never leaves a partial.
        stale = _OUT_DIR / f"{result['invoice_id']}_coding_sheet.md"
        stale.unlink(missing_ok=True)
        logger.info(f"[mat] cross-check floor held: {discrepancy} "
                    f"(round {rounds}, exhausted={result['match_retry_exhausted']})")
        return result

    # Tied out → render the sheet and stash the coded entry for the finisher.
    result["match_retry_exhausted"] = False
    coded = {**inv, "gl_account": result["gl_account"], "cost_center": result["cost_center"],
             "amount_cents": amount_cents, "vendor_name": vendor.get("vendor_name", "")}
    result["artifact_path"] = _render_sheet(coded, result["gl_account"], result["cost_center"])
    stash_put(sid, coded)
    logger.info(f"[mat] coded {result['invoice_id']} -> {result['gl_account']} "
                f"conf={result['confidence']} -> {result['artifact_path']}")
    return result


@chain(enforce_cross_check_floor)
async def matching_gl_agent(llm_response: Any, input_data: dict, context: dict) -> dict:
    out = llm_response if isinstance(llm_response, dict) else {}
    out.setdefault("match_ok", False)
    out.setdefault("cross_check_ok", False)
    out.setdefault("discrepancy", "")
    out.setdefault("gl_account", "")
    out.setdefault("cost_center", "")
    out.setdefault("confidence", 0.8)
    out.setdefault("invoice_id", "")
    out.setdefault("vendor_id", "")
    out.setdefault("amount_cents", 0)
    out.setdefault("anomaly_risk", False)
    out.setdefault("artifact_path", "")
    out.setdefault("match_retry_exhausted", False)
    input_data = input_data if isinstance(input_data, dict) else {}
    out["_input"] = input_data
    out["_invoice"] = _recover_invoice(input_data, context)
    return out
