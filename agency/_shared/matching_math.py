"""Deterministic 3-way-match tolerance arithmetic.

The matching floor the LLM cannot waive. Reconciles invoice ↔ purchase
order ↔ goods receipt line by line against fixed tolerances:

  * quantity:  abs variance must be within QTY_TOLERANCE_UNITS
  * unit price: variance must be within PRICE_TOLERANCE_PCT

A line outside tolerance is a gap; any gap fails the 3-way match. These
constants ARE the firm's matching policy — a controller tunes them here,
not by re-prompting a model.

Production reads the PO + goods-receipt from the ERP (store.get_po in
dev); the arithmetic below is identical in both.
"""
from __future__ import annotations

from typing import Any

QTY_TOLERANCE_UNITS = 0          # exact quantity match required by default
PRICE_TOLERANCE_PCT = 0.02       # 2% unit-price tolerance
PO_MATCH_FLOOR = 0.90            # line-match ratio that clears PO match


def _inv_index(line_items: list[Any]) -> dict[str, dict[str, Any]]:
    """Index invoice lines by sku (fallback: description)."""
    idx: dict[str, dict[str, Any]] = {}
    for li in line_items or []:
        if not isinstance(li, dict):
            continue
        key = str(li.get("sku") or li.get("description") or "").strip().lower()
        if key:
            idx[key] = li
    return idx


def po_line_match(po: dict[str, Any], line_items: list[Any]) -> dict[str, Any]:
    """Phase 1 — match invoice lines against the PO lines.

    Grounded in the real PO record (store-backed in dev). Returns the
    PO-match shape. po_found is False when the PO is unknown.
    """
    if not po:
        return {
            "po_found": False, "po_id": "", "lines_matched": 0,
            "lines_total": len(line_items or []), "match_percentage": 0.0,
            "discrepancies": ["po_not_found"],
        }

    po_lines = po.get("lines") or []
    lines_total = max(len(po_lines), 1)
    inv_idx = _inv_index(line_items)
    matched = 0
    discrepancies: list[str] = []

    for pol in po_lines:
        sku = str(pol.get("sku") or pol.get("description") or "").strip().lower()
        inv = inv_idx.get(sku)
        if inv is None:
            discrepancies.append(f"missing_invoice_line:{sku or '?'}")
            continue
        # quantity check (within tolerance)
        try:
            qv = abs(int(inv.get("quantity") or 0) - int(pol.get("quantity") or 0))
        except (TypeError, ValueError):
            qv = 0
        if qv > QTY_TOLERANCE_UNITS:
            discrepancies.append(f"qty_variance:{sku}:{qv}")
            continue
        matched += 1

    match_pct = matched / lines_total
    return {
        "po_found": True,
        "po_id": po.get("po_number", ""),
        "lines_matched": matched,
        "lines_total": lines_total,
        "match_percentage": round(match_pct, 4),
        "discrepancies": discrepancies,
    }


def three_way_match(po: dict[str, Any], line_items: list[Any]) -> dict[str, Any]:
    """Phase 2 — reconcile invoice ↔ PO ↔ goods receipt.

    Compares invoiced quantity/price against the PO and the goods
    receipt within tolerance. Any line outside tolerance is a gap; any
    gap fails the match. receipt_found is False when no receipt exists.
    """
    receipt = (po or {}).get("goods_receipt")
    if not po:
        return {"three_way_matched": False, "receipt_found": False,
                "quantity_variance": 0, "price_variance_pct": 0.0,
                "gaps": ["po_missing_cannot_reconcile"]}
    if not receipt:
        return {"three_way_matched": False, "receipt_found": False,
                "quantity_variance": 0, "price_variance_pct": 0.0,
                "gaps": ["no_goods_receipt_booked"]}

    inv_idx = _inv_index(line_items)
    rcpt_idx = {str(rl.get("sku") or "").strip().lower(): rl
                for rl in (receipt.get("lines") or []) if isinstance(rl, dict)}

    gaps: list[str] = []
    worst_qty_var = 0
    worst_price_var = 0.0

    for pol in po.get("lines") or []:
        sku = str(pol.get("sku") or "").strip().lower()
        po_qty = int(pol.get("quantity") or 0)
        po_price = int(pol.get("unit_price_cents") or 0)
        inv = inv_idx.get(sku) or {}
        rcpt = rcpt_idx.get(sku) or {}

        inv_qty = int(inv.get("quantity") or 0)
        rcpt_qty = int(rcpt.get("quantity") or 0)
        # invoiced unit price in cents (line total / qty when available)
        inv_price = po_price
        if inv.get("unit_price") is not None:
            try:
                inv_price = int(round(float(inv["unit_price"]) * 100))
            except (TypeError, ValueError):
                inv_price = po_price

        # quantity: invoice vs goods-receipt (you pay for what arrived)
        qv = abs(inv_qty - rcpt_qty) if rcpt else abs(inv_qty - po_qty)
        worst_qty_var = max(worst_qty_var, qv)
        if qv > QTY_TOLERANCE_UNITS:
            gaps.append(f"quantity_variance:{sku}:inv{inv_qty}_vs_recv{rcpt_qty}")

        # price: invoice vs PO within pct tolerance
        if po_price:
            pv = abs(inv_price - po_price) / po_price
            worst_price_var = max(worst_price_var, pv)
            if pv > PRICE_TOLERANCE_PCT:
                gaps.append(f"price_variance:{sku}:{pv*100:.1f}pct")

    matched = not gaps
    return {
        "three_way_matched": matched,
        "receipt_found": True,
        "quantity_variance": worst_qty_var,
        "price_variance_pct": round(worst_price_var, 4),
        "gaps": gaps,
    }
