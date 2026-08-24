"""Poe — ERP Poster & Ledger Liaison (the FINISHER / actuator).

auto_discover matches `erp_poster_agent`. Poe sits BEHIND the human approval
gate. It never approves or pays — it re-checks that a human actually approved,
posts the entry to the ERP, and records an append-only ledger row. It is the
concrete "@chain fails closed":

  a) approval RE-CHECK — reaching this agent does not mean approved. An
     allow-token allowlist + block-token denylist; an UNKNOWN verdict fails closed.
  b) deterministic invariants a human can't waive (an invoice on record, a GL
     account, a positive amount).
  c) post the entry + render the posting pack to ./out; on a tripped floor,
     DELETE the partial and record `status: blocked` with reasons — never a
     half-posted entry.
  d) dev-store recovery — after the HITL gate the upstream payload is dropped,
     so Poe recovers the coded invoice from the session stash / dev store.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from leafmesh import LeafMeshLogger, chain

try:
    from ._shared import store
    from ._shared.erp_poster import dispatch_erp
    from ._shared.session_stash import resolve_session_id, stash_get
except ImportError:  # pragma: no cover
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _shared import store  # type: ignore
    from _shared.erp_poster import dispatch_erp  # type: ignore
    from _shared.session_stash import resolve_session_id, stash_get  # type: ignore

logger = LeafMeshLogger(__name__)

_OUT_DIR = Path("./out")
_APPROVE_TOKENS = frozenset({"approve", "approved", "sign", "signed", "yes", "ok"})
_BLOCK_TOKENS = frozenset({"reject", "rejected", "no", "deny", "denied",
                           "held", "held_for_review", "hold", "halt"})


def _signal(input_data: dict) -> str:
    """The human verdict, from wherever the SDK delivered it (shape-tolerant)."""
    sources = [input_data]
    up = input_data.get("upstream_yields")
    if isinstance(up, dict):
        sources.extend(v for v in up.values() if isinstance(v, dict))
        sources.append(up)
    for src in sources:
        for key in ("human_message", "human_decision", "decision", "message"):
            val = src.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip().lower()
    return ""


def _is_approved(input_data: dict) -> bool:
    """Fail closed: only an explicit approve token counts; unknown => not approved."""
    sig = _signal(input_data)
    if sig in _BLOCK_TOKENS:
        return False
    return sig in _APPROVE_TOKENS


def _is_blocked(input_data: dict) -> bool:
    return _signal(input_data) in _BLOCK_TOKENS


def _recover_invoice(input_data: dict, context: dict) -> dict[str, Any]:
    """Recover the coded invoice across the HITL boundary: upstream -> stash -> store."""
    up = input_data.get("upstream_yields") or {}
    for src in (input_data, up if isinstance(up, dict) else None):
        if isinstance(src, dict) and src.get("gl_account") and src.get("invoice_id"):
            return src
    sid = resolve_session_id(input_data, context)
    stashed = stash_get(sid)
    if stashed.get("invoice_id"):
        return stashed
    inv_id = str(input_data.get("invoice_id") or "")
    return store.find_invoice(inv_id) or {"invoice_id": inv_id}


def _to_int(v: Any) -> int:
    try:
        return int(round(float(v or 0)))
    except (TypeError, ValueError):
        return 0


def _render_pack(inv: dict, entry_id: str) -> str:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    inv_id = str(inv.get("invoice_id") or "invoice")
    path = _OUT_DIR / f"{inv_id}_posted_pack.md"
    amt = _to_int(inv.get("amount_cents"))
    path.write_text(
        f"# Posted entry — {inv_id}\n"
        f"ERP entry id: {entry_id}\n"
        f"Vendor: {inv.get('vendor_name') or inv.get('vendor_id','')}\n"
        f"GL account: {inv.get('gl_account','')}    Cost center: {inv.get('cost_center','')}\n"
        f"Amount: ${amt/100:,.2f}\n\n"
        f"_Posted to the ERP and recorded in the append-only ledger._\n",
        encoding="utf-8")
    return str(path)


def _delete_partial(result: dict) -> None:
    p = result.get("pack_path")
    if p:
        Path(p).unlink(missing_ok=True)
        result["pack_path"] = ""


def enforce_posting_floor(result: Any, context: dict) -> dict:
    """The finisher floor — post ONLY on a real approval + real invariants."""
    result = result if isinstance(result, dict) else {}
    result.setdefault("block_reasons", [])
    inv = result.get("_invoice") or {}
    input_data = result.get("_input") or {}

    if not result.get("approved"):
        result["status"] = "blocked"
        if _is_blocked(input_data):
            result["block_reasons"].append("approver rejected / held")
        else:
            result["block_reasons"].append("no explicit approval on record (fail closed)")
        _delete_partial(result)
        logger.info(f"[poe] posting floor BLOCKED: {result['block_reasons']}")
        return result

    # Deterministic invariants a human cannot waive.
    inv_id = str(inv.get("invoice_id") or "")
    gl_account = str(inv.get("gl_account") or "")
    amount_cents = _to_int(inv.get("amount_cents"))
    reasons = []
    if not inv_id:
        reasons.append("no invoice recoverable after approval")
    if not gl_account:
        reasons.append("no GL account on the coded entry")
    if amount_cents <= 0:
        reasons.append("amount is not positive")
    if reasons:
        result.update(status="blocked", approved=False, posted=False)
        result["block_reasons"].extend(reasons)
        _delete_partial(result)
        logger.error(f"[poe] REFUSING to post {inv_id or '?'}: {reasons}")
        return result

    # Passed the floor — post to the ERP, render the pack, record the ledger.
    posted = dispatch_erp(config={}, payload={
        "invoice_id": inv_id, "vendor_id": inv.get("vendor_id", ""),
        "gl_account": gl_account, "cost_center": inv.get("cost_center", ""),
        "tax_code": inv.get("tax_code", "NONE"), "amount_cents": amount_cents,
        "recommended_pay_date": inv.get("due_date", ""),
    })
    entry_id = str(posted.get("erp_entry_id") or "")
    result.update(posted=True, status="posted", erp_entry_id=entry_id,
                  invoice_id=inv_id, vendor_name=str(inv.get("vendor_name") or ""),
                  amount_cents=amount_cents)
    result["pack_path"] = _render_pack({**inv, "amount_cents": amount_cents}, entry_id)
    store.record_posted({
        "invoice_id": inv_id, "vendor_id": inv.get("vendor_id", ""),
        "amount_cents": amount_cents, "due_date": inv.get("due_date", ""),
        "erp_entry_id": entry_id, "posted_at_utc": posted.get("posted_at_utc", ""),
    })
    logger.info(f"[poe] posted {inv_id} -> {entry_id} -> {result['pack_path']}")
    return result


@chain(enforce_posting_floor)
async def erp_poster_agent(llm_response: Any, input_data: dict, context: dict) -> dict:
    input_data = input_data if isinstance(input_data, dict) else {}
    inv = _recover_invoice(input_data, context)
    out: dict[str, Any] = {
        "approved": _is_approved(input_data), "posted": False, "erp_entry_id": "",
        "pack_path": "", "status": "", "block_reasons": [],
        "invoice_id": str(inv.get("invoice_id") or ""),
        "vendor_name": str(inv.get("vendor_name") or ""),
        "amount_cents": _to_int(inv.get("amount_cents")),
        "_input": input_data, "_invoice": inv,
    }
    return out
