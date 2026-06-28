"""invoice_intake_agent — multi-source normalizer + completeness floor.

The LLM (configs/config.yaml) does the normalisation + field extraction;
this micro-module owns the two things the LLM must NOT be trusted to do:

@pre_compose  load_intake_context — pull the VENDOR-MASTER record and the
              vendor's PRIOR-INVOICE history from the source store (the
              ERP vendor master / AP ledger in prod) BEFORE the LLM runs,
              so extraction is grounded in what we already know about the
              vendor (canonical id, default terms, whether the vendor is
              first-time-seen) rather than whatever the document echoes.
              Self-reliant context pull — the same pattern as matching /
              payment_risk / gl_coding.

@chain        enforce_intake_floor — the deterministic COMPLETENESS +
              NORMALISATION floor. The LLM cannot present a structurally
              broken invoice as clean:
                · a vendorless invoice (no vendor_id AND no vendor_name)
                  or an amountless invoice (amount_cents <= 0) is FLAGGED
                  intake_incomplete and the blocking reason recorded — it
                  must not silently flow into matching / payment.
                · the deterministic routing-flag floor (the policy the
                  prompt states) is RE-ASSERTED in Python: >= $1K with a
                  PO ⇒ requires_po_match; >= $5K ⇒ requires_3way_match;
                  >= $10K / first-time vendor / round-thousand ⇒
                  anomaly_risk. The LLM may RAISE a flag, never LOWER one.
                · invoice_id is derived deterministically when the
                  document carries none (so dedupe/idempotency has a key).
                · a HARD-ESCALATION condition — an incomplete invoice, or
                  a first-time vendor over the $10K anomaly line — sets
                  intake_hard_escalate so the wire can route to review
                  instead of trusting the LLM's flags.

Everything else (the actual field extraction, line-item parsing, the
raw_payload preservation) stays prompt-driven and SDK yields-filled.
"""
from __future__ import annotations

import json
from typing import Any

from leafmesh import LeafMeshLogger, chain, pre_compose

from agency._shared import store

logger = LeafMeshLogger(__name__)

# Deterministic policy thresholds (cents) — mirror the prompt rules so the
# floor and the prompt cannot drift apart.
_PO_MATCH_FLOOR_CENTS = 100000      # >= $1K with a PO → PO match
_THREE_WAY_FLOOR_CENTS = 500000     # >= $5K → 3-way match
_ANOMALY_FLOOR_CENTS = 1000000      # >= $10K → anomaly risk
_ROUND_THOUSAND_CENTS = 100000      # divisible by this ⇒ round-thousand


def _up(input_data: dict[str, Any], key: str, default: Any = None) -> Any:
    """Upstream read: flat input_data, then flat upstream_yields."""
    if input_data.get(key) not in (None, ""):
        return input_data[key]
    uy = input_data.get("upstream_yields")
    if isinstance(uy, dict) and uy.get(key) not in (None, ""):
        return uy[key]
    return default


def _payload(input_data: dict[str, Any]) -> dict[str, Any]:
    p = input_data.get("payload")
    return p if isinstance(p, dict) else {}


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# ── @pre_compose — pull the vendor record + prior-invoice context ─────
def load_intake_context(data: Any, ctx: Any) -> dict[str, Any]:
    """Fetch the vendor-master record + the vendor's prior posted history
    BEFORE extraction. PROD: ERP vendor-master read + AP-ledger query.

    The vendor id may arrive on the flat input, in upstream_yields, or in
    the raw source payload — try each so the context pull is self-reliant
    regardless of how intake was invoked.
    """
    input_data = data if isinstance(data, dict) else {}
    payload = _payload(input_data)
    vendor_id = str(
        _up(input_data, "vendor_id", "") or payload.get("vendor_id") or ""
    )
    vendor = store.get_vendor(vendor_id) if vendor_id else {}
    prior = store.posted_for_vendor(vendor_id) if vendor_id else []
    return {
        "vendor": vendor,
        "vendor_known": bool(vendor),
        "vendor_first_time": not bool(prior),
        "vendor_default_terms": str(vendor.get("payment_terms") or "") if vendor else "",
        "vendor_requires_po": bool(vendor.get("requires_po")) if vendor else False,
        "prior_invoice_count": len(prior),
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


def _derive_invoice_id(result: dict[str, Any]) -> str:
    """Deterministic id when the document carries none — dedupe/idempotency
    needs a stable key. Mirrors the prompt's derivation rule."""
    seid = str(result.get("source_event_id") or "").strip()
    if seid:
        return f"inv-{seid}"
    vendor = str(result.get("vendor_id") or result.get("vendor_name") or "unknown").strip()
    vendor = vendor.lower().replace(" ", "-")
    due = str(result.get("due_date") or "nodate").strip()
    return f"inv-{vendor}-{due}"


# ── @chain — the deterministic COMPLETENESS + NORMALISATION floor ─────
async def enforce_intake_floor(result: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Re-assert completeness + the routing-flag policy over the LLM's
    extraction. The LLM cannot pass a vendorless/amountless invoice as
    clean, nor lower a routing flag the policy demands.

    The body attaches the @pre_compose vendor context as private `_intake_*`
    keys (it has input_data + context); this floor consumes + strips them,
    so it fires identically through the SDK runtime and on a direct call.
    """
    if not isinstance(result, dict):
        result = {}

    vendor_first_time = bool(result.pop("_intake_vendor_first_time", False))

    amount = _int(result.get("amount_cents"))
    vendor_id = str(result.get("vendor_id") or "").strip()
    vendor_name = str(result.get("vendor_name") or "").strip()
    po_number = str(result.get("po_number") or "").strip()

    # Derive a stable invoice_id when missing (idempotency / dedupe key).
    if not str(result.get("invoice_id") or "").strip():
        result["invoice_id"] = _derive_invoice_id(result)

    # ── Completeness floor ───────────────────────────────────────────
    blocking: list[str] = []
    if not vendor_id and not vendor_name:
        blocking.append("missing_vendor")
    if amount <= 0:
        blocking.append("missing_or_zero_amount")

    result["intake_incomplete"] = bool(blocking)
    result["intake_blocking_reasons"] = blocking

    # ── Routing-flag floor (policy the LLM may RAISE, never LOWER) ────
    # >= $1K with a PO → PO match.
    if amount >= _PO_MATCH_FLOOR_CENTS and po_number:
        result["requires_po_match"] = True
    else:
        result["requires_po_match"] = bool(result.get("requires_po_match"))

    # >= $5K → 3-way match.
    if amount >= _THREE_WAY_FLOOR_CENTS:
        result["requires_3way_match"] = True
    else:
        result["requires_3way_match"] = bool(result.get("requires_3way_match"))

    # >= $10K, first-time / unknown vendor, or a round thousand → anomaly.
    round_thousand = amount > 0 and amount % _ROUND_THOUSAND_CENTS == 0
    anomaly = (
        amount >= _ANOMALY_FLOOR_CENTS
        or not vendor_id
        or vendor_first_time
        or round_thousand
    )
    if anomaly:
        result["anomaly_risk"] = True
    else:
        result["anomaly_risk"] = bool(result.get("anomaly_risk"))

    # ── Hard-escalation condition (the LLM can't clear it) ───────────
    # An incomplete invoice, or a first-time vendor billing over the
    # anomaly line, must route to review rather than flow on the LLM's say.
    result["intake_hard_escalate"] = bool(
        result["intake_incomplete"]
        or (vendor_first_time and amount >= _ANOMALY_FLOOR_CENTS)
    )

    if result["intake_incomplete"]:
        logger.warning(
            f"[intake] COMPLETENESS floor: invoice {result.get('invoice_id')} "
            f"incomplete — {blocking}"
        )
    if result["intake_hard_escalate"]:
        logger.warning(
            f"[intake] HARD-ESCALATE: invoice {result.get('invoice_id')} "
            f"(first_time={vendor_first_time} amount_cents={amount})"
        )
    return result


@pre_compose(context_processor=load_intake_context)
@chain(enforce_intake_floor)
async def invoice_intake_agent(
    llm_response: dict[str, Any] | str | None,
    input_data: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Surface the LLM's normalised extraction; the chain enforces the
    completeness + routing-flag floor."""
    input_data = input_data if isinstance(input_data, dict) else {}
    result = _parse(llm_response)
    if not isinstance(result, dict):
        result = {}
    result = dict(result)

    # Resolve the vendor-first-time signal here (we have the @pre_compose
    # context + input_data) and hand it to the floor via a private carry
    # key. Prefer the prepared context; fall back to the store on a direct
    # call so the floor fires end-to-end in tests.
    cctx = _ctx(context)
    if cctx:
        vendor_first_time = bool(cctx.get("vendor_first_time", True))
    else:
        vid = str(result.get("vendor_id") or _up(input_data, "vendor_id", "") or "")
        vendor_first_time = not bool(store.posted_for_vendor(vid)) if vid else True
    result["_intake_vendor_first_time"] = vendor_first_time
    return result
