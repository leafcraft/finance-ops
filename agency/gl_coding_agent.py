"""gl_coding_agent — micro-module (pre-compose + post-process).

The LLM (configs/config.yaml) proposes GL account / cost center / tax
code; this module owns the deterministic pieces:

@pre_compose  load_chart — pull the active CHART OF ACCOUNTS and the
              vendor's standing default GL mapping from the source store
              (ERP GL master in prod) so the LLM codes against the real
              chart even before the chart_of_accounts knowledge group is
              seeded. Self-reliant context pull.
post-process  (the agent body, run after the LLM):
              · deterministic keyword FALLBACK chart when the LLM returns
                no coding (unseeded knowledge group);
              · the GL-VALIDITY floor — a coded account that is NOT an
                active account in the chart is REJECTED: confidence is
                forced below the 0.7 wire floor so the approval matrix
                routes it to the Controller, never the AP clerk. The LLM
                cannot post to an account that does not exist;
              · prefer the vendor's standing default mapping;
              · stash the invoice + GL context per session so erp_poster
                recovers it after the HITL approval gate (fail-closed).
"""
from __future__ import annotations

import json
import os
from typing import Any

from leafmesh import LeafMeshLogger, pre_compose

from agency._shared import store
from agency._shared.session_stash import resolve_session_id, stash_put

logger = LeafMeshLogger(__name__)

DEFAULT_COST_CENTER = os.getenv("GL_DEFAULT_COST_CENTER", "CC-OPEX")
_VALIDITY_FLOOR_CONF = 0.4   # below the 0.7 wire floor → Controller

# Deterministic fallback chart — keyword → gl_account. Used only when the
# LLM returns no coding. Hits carry 0.72 (explainable rule); misses 0.4.
_FALLBACK_CHART: tuple[tuple[tuple[str, ...], str], ...] = (
    (("subscription", "saas", "software", "cloud", "license"), "6500-Software-and-SaaS"),
    (("consulting", "professional services", "advisory"),       "6610-Professional-Services"),
    (("freight", "shipping", "logistics", "carrier"),           "5400-Freight-and-Logistics"),
    (("rent", "lease", "office space"),                         "6200-Rent-and-Facilities"),
    (("marketing", "advertising", "campaign"),                  "6700-Marketing"),
    (("hardware", "equipment", "laptop", "server"),             "1500-Equipment"),
    (("travel", "hotel", "airfare"),                            "6800-Travel"),
)


def _fallback_code(line_items: list[Any]) -> tuple[str, float]:
    text = " ".join(
        (li.get("description", "") if isinstance(li, dict) else str(li))
        for li in line_items
    ).lower()
    for keywords, gl_account in _FALLBACK_CHART:
        if any(k in text for k in keywords):
            return gl_account, 0.72
    return "6999-Uncategorized", 0.4


def _upstream_line_items(input_data: dict[str, Any]) -> list[Any]:
    items = input_data.get("line_items")
    if isinstance(items, list):
        return items
    uy = input_data.get("upstream_yields")
    if isinstance(uy, dict):
        if isinstance(uy.get("line_items"), list):
            return uy["line_items"]
        for v in uy.values():
            if isinstance(v, dict) and isinstance(v.get("line_items"), list):
                return v["line_items"]
    return []


def _vendor_id(input_data: dict[str, Any]) -> str:
    if input_data.get("vendor_id"):
        return str(input_data["vendor_id"])
    uy = input_data.get("upstream_yields")
    if isinstance(uy, dict):
        if uy.get("vendor_id"):
            return str(uy["vendor_id"])
        for v in uy.values():
            if isinstance(v, dict) and v.get("vendor_id"):
                return str(v["vendor_id"])
    return ""


# ── @pre_compose — pull the active chart + vendor default mapping ─────
def load_chart(data: Any, ctx: Any) -> dict[str, Any]:
    """Fetch the GL chart + the vendor's standing default. PROD: ERP GL master."""
    input_data = data if isinstance(data, dict) else {}
    vendor = store.get_vendor(_vendor_id(input_data))
    return {
        "active_accounts": store.chart_accounts(),
        "vendor_default_gl": str(vendor.get("default_gl_account") or "") if vendor else "",
        "vendor_default_cost_center": str(vendor.get("default_cost_center") or "") if vendor else "",
    }


def _ctx(context: Any) -> dict[str, Any]:
    prepared = (context or {}).get("prepared_data", {}) if isinstance(context, dict) else {}
    c = prepared.get("context", {}) if isinstance(prepared, dict) else {}
    return c if isinstance(c, dict) else {}


@pre_compose(context_processor=load_chart)
async def gl_coding_agent(
    llm_response: dict[str, Any] | str | None,
    input_data: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Fallback-code, enforce the GL-validity floor, then stash for post-HITL."""
    if isinstance(llm_response, str):
        try:
            llm_response = json.loads(llm_response)
        except (ValueError, TypeError):
            llm_response = {}
    if not isinstance(llm_response, dict):
        llm_response = {}
    result = dict(llm_response)
    input_data = input_data if isinstance(input_data, dict) else {}
    cctx = _ctx(context)
    # Self-sufficient fallback: when @pre_compose context isn't present
    # (e.g. a direct call), read the vendor default from the store here.
    if not cctx:
        vendor = store.get_vendor(_vendor_id(input_data))
        cctx = {
            "vendor_default_gl": str(vendor.get("default_gl_account") or "") if vendor else "",
            "vendor_default_cost_center": str(vendor.get("default_cost_center") or "") if vendor else "",
        }

    # Deterministic keyword fallback — only when the LLM returned no coding.
    if not result.get("gl_account"):
        # Prefer the vendor's standing default before the keyword chart.
        vendor_default = cctx.get("vendor_default_gl") or ""
        if vendor_default and store.gl_account_exists(vendor_default):
            result["gl_account"] = vendor_default
            result["confidence"] = 0.8
            logger.info(f"[gl-coding] used vendor default {vendor_default}")
        else:
            gl_account, confidence = _fallback_code(_upstream_line_items(input_data))
            result["gl_account"] = gl_account
            result["confidence"] = confidence
            logger.info(
                f"[gl-coding] LLM returned no gl_account — deterministic "
                f"fallback {gl_account} (conf={confidence:.2f})"
            )
    if not result.get("cost_center"):
        result["cost_center"] = cctx.get("vendor_default_cost_center") or DEFAULT_COST_CENTER

    # ── GL-VALIDITY FLOOR — the LLM can't post to a non-existent account ─
    # A coded account that isn't an active account in the chart is invalid:
    # force confidence below the 0.7 wire floor so the approval matrix
    # routes the invoice to the Controller, never the AP clerk.
    coded = str(result.get("gl_account") or "")
    if coded and not store.gl_account_exists(coded):
        try:
            conf = float(result.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        result["confidence"] = min(conf, _VALIDITY_FLOOR_CONF)
        result["gl_validity"] = "invalid_account"
        logger.warning(
            f"[gl-coding] GL-VALIDITY floor: {coded!r} not an active chart "
            f"account → confidence forced to {result['confidence']} (→ Controller)"
        )
    else:
        result["gl_validity"] = "valid"

    # Stash the invoice + GL context keyed by session — after the human
    # approval gate the SDK resumes with only human-response meta fields;
    # erp_poster recovers the payload from here (fail-closed if absent).
    session_id = resolve_session_id(input_data, context)
    stash_put(session_id, {
        k: result.get(k) for k in (
            "invoice_id", "vendor_id", "vendor_name", "amount_cents",
            "due_date", "gl_account", "cost_center", "tax_code",
            "anomaly_risk", "confidence",
        )
    })
    return result
