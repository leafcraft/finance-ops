"""erp_poster_agent — programmatic ERP dispatcher.

KEPT AS PYTHON (class: actuator) — real side effect (ERP write) plus
fail-closed post-HITL recovery; an entry with no invoice_id in the ERP
is an audit finding, not a success.

Calls the right ERP integration based on env (ERP_TYPE) and returns
the entry ID + timestamp. The actual ERP-specific HTTP / RPC lives in
agency/_shared/erp_poster.py so this file stays small.

This agent is usually invoked by a HUMAN approval (ap_clerk /
controller / cfo). The SDK resumes the chain with only the
human-response meta fields, so the invoice payload is recovered from
the per-session stash that gl_coding_agent wrote before the gate.
FAIL-CLOSED: if no invoice can be recovered, nothing is posted.
"""
from __future__ import annotations

from typing import Any

from leafmesh import LeafMeshLogger

from agency._shared import store
from agency._shared.erp_poster import dispatch_erp
from agency._shared.session_stash import resolve_session_id, stash_get

logger = LeafMeshLogger(__name__)

_FIELDS = ("invoice_id", "vendor_id", "vendor_name", "gl_account",
           "cost_center", "tax_code", "amount_cents", "due_date",
           "recommended_pay_date")


def _lookup(key: str, *sources: Any) -> Any:
    """First non-empty value for key across dict sources."""
    for src in sources:
        if isinstance(src, dict) and src.get(key) not in (None, ""):
            return src[key]
    return None


async def erp_poster_agent(
    llm_response: dict[str, Any] | str | None,
    input_data: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Post the invoice. Returns the standardised entry shape."""
    session_id = resolve_session_id(input_data, context)
    stashed = stash_get(session_id)
    # Direct/scheduler calls deliver the payload in input_data; after a
    # human gate the gl_coding stash is the recovery source.
    sources = (input_data, input_data.get("upstream_yields"),
               input_data.get("human_data"), stashed)

    payload = {k: _lookup(k, *sources) for k in _FIELDS}
    payload["recommended_pay_date"] = payload.get("recommended_pay_date") or ""

    if not payload.get("invoice_id"):
        logger.error(
            f"[erp] REFUSING to post — no invoice_id recoverable "
            f"(session={session_id or '?'}). Posting an anonymous entry "
            f"would corrupt the ledger."
        )
        return {
            "erp_entry_id": "",
            "posted_at_utc": "",
            "posted": False,
            "erp_type": "",
            "invoice_id": "",
            "vendor_name": "",
            "amount_cents": 0,
            "error": "no invoice payload recoverable after approval — "
                     "gl_coding stash missing for this session",
        }

    result = dispatch_erp(config={}, payload=payload)
    # Echo invoice identity in the yields so the audit line answers
    # "WHICH invoice got approved and posted".
    result["invoice_id"] = str(payload.get("invoice_id") or "")
    result["vendor_name"] = str(payload.get("vendor_name") or "")
    try:
        result["amount_cents"] = int(payload.get("amount_cents") or 0)
    except (TypeError, ValueError):
        result["amount_cents"] = 0
    result["error"] = ""

    # Record into the dev posted-ledger so the duplicate pre-flight sees
    # real history accumulate (a re-submission of this invoice now hits
    # the dedupe floor). PROD: no-op — the ERP already holds the post.
    store.record_posted({
        "invoice_id": result["invoice_id"],
        "vendor_id": str(payload.get("vendor_id") or ""),
        "amount_cents": result["amount_cents"],
        "due_date": str(payload.get("due_date") or ""),
        "erp_entry_id": str(result.get("erp_entry_id") or ""),
        "posted_at_utc": str(result.get("posted_at_utc") or ""),
    })
    return result
