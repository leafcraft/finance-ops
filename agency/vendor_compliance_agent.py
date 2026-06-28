"""vendor_compliance_agent — KYC + onboarding with a hard compliance floor.

The LLM (configs/config.yaml) does the KYC narrative, beneficial-owner
extraction, and the onboarding profile; this micro-module owns the
deterministic compliance controls a regulator reads — the things the LLM
must NOT be trusted to waive:

@pre_compose  load_vendor_context — pull the VENDOR-MASTER record (if the
              counterparty already exists) from the source store before
              the screen runs, so a known-vendor re-screen is grounded in
              the record on file. PROD: ERP vendor master + screening
              provider. Self-reliant context pull.

@chain        enforce_compliance_floor — the deterministic SANCTIONS /
              HIGH-RISK-COUNTRY / MISSING-TAX-FORM floor
              (agency/_shared/compliance_screen). Any hit FORCES compliance
              review — the LLM cannot clear it:
                · a sanctions hit (declared OR caught by the deterministic
                  name screen) → overall_status=rejected, requires_filing_sar,
                  risk_score=1.0, requires_compliance_review, ofac_clear=false,
                  onboarded_to_master=false.
                · a high-risk jurisdiction → high_risk_country=true,
                  requires_compliance_review (enhanced due diligence).
                · onboarding with no tax form on file → missing_fields
                  carries "tax_form_kind", requires_human_review, never
                  onboarded_to_master.
                · onboarded_to_master is allowed True ONLY when the screen
                  is clean AND missing_fields is empty AND ofac_clear — the
                  LLM cannot flip a flagged counterparty into the master.

Everything else (the KYC verdict narrative, beneficial-owner analysis,
the onboarding briefing) stays prompt-driven and SDK yields-filled.
"""
from __future__ import annotations

import json
from typing import Any

from leafmesh import LeafMeshLogger, chain, pre_compose

from agency._shared import store
from agency._shared.compliance_screen import screen_counterparty

logger = LeafMeshLogger(__name__)

# Onboarding required-field floor — every field a vendor needs before any
# payment path. tax_form_kind is screened separately (it must be a VALID
# form, not merely present), so it is enforced via the screen, not here.
# The country may arrive as country_code OR (flat-yield) jurisdiction.
_ONBOARDING_REQUIRED = ("vendor_id", "legal_name", "bank_routing_verified")


def _up(input_data: dict[str, Any], key: str, default: Any = None) -> Any:
    if input_data.get(key) not in (None, ""):
        return input_data[key]
    uy = input_data.get("upstream_yields")
    if isinstance(uy, dict) and uy.get(key) not in (None, ""):
        return uy[key]
    return default


def _parse(resp: Any) -> dict[str, Any]:
    if isinstance(resp, dict):
        return resp
    if isinstance(resp, str):
        try:
            return json.loads(resp)
        except (ValueError, TypeError):
            return {}
    return {}


def _ctx(context: Any) -> dict[str, Any]:
    prepared = (context or {}).get("prepared_data", {}) if isinstance(context, dict) else {}
    c = prepared.get("context", {}) if isinstance(prepared, dict) else {}
    return c if isinstance(c, dict) else {}


# ── @pre_compose — pull the existing vendor-master record (if any) ────
def load_vendor_context(data: Any, ctx: Any) -> dict[str, Any]:
    """Fetch the on-file vendor record so a re-screen is grounded. PROD:
    ERP vendor master + the screening provider's prior-screen cache."""
    input_data = data if isinstance(data, dict) else {}
    counterparty_id = str(
        _up(input_data, "counterparty_id", "") or _up(input_data, "vendor_id", "") or ""
    )
    vendor = store.get_vendor(counterparty_id) if counterparty_id else {}
    return {
        "vendor_on_file": vendor,
        "vendor_known": bool(vendor),
        "on_file_country": str(vendor.get("country_code") or "") if vendor else "",
    }


# ── @chain — the deterministic compliance floor (LLM can't clear it) ──
async def enforce_compliance_floor(result: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Run the deterministic sanctions / geography / tax-form screen and
    force the compliance verdict. The LLM may RAISE risk; it can never
    LOWER a flagged counterparty to a cleared state.

    The body resolves the mode + the screen inputs and attaches them as
    private `_cmp_*` keys; this floor consumes + strips them so it fires
    identically through the SDK runtime and on a direct call.
    """
    if not isinstance(result, dict):
        result = {}

    mode = str(result.pop("_cmp_mode", "") or result.get("compliance_mode") or "kyc_only")
    legal_name = str(result.pop("_cmp_legal_name", "") or result.get("legal_name") or "")
    country = str(result.pop("_cmp_country", "") or result.get("jurisdiction") or "")
    tax_form = str(result.pop("_cmp_tax_form", "") or result.get("tax_form_kind") or "")
    onboarding = mode == "onboarding"

    screen = screen_counterparty(
        legal_name,
        country,
        declared_sanctions_hits=result.get("sanctions_hits"),
        tax_form_kind=tax_form,
        onboarding=onboarding,
    )

    result["compliance_mode"] = mode
    result["sanctions_hits"] = screen["sanctions_hits"]
    result["high_risk_country"] = bool(screen["high_risk_country"] or result.get("high_risk_country"))

    # ── Sanctions hit — the hardest floor: reject + SAR, never clear ──
    if screen["has_sanctions_hit"]:
        result["overall_status"] = "rejected"
        result["requires_filing_sar"] = True
        result["requires_compliance_review"] = True
        result["risk_score"] = 1.0
        result["ofac_clear"] = False
        logger.warning(
            f"[compliance] SANCTIONS floor fired ({screen['sanctions_hits']}) "
            f"name={legal_name!r} → rejected + SAR"
        )
    else:
        result.setdefault("requires_filing_sar", False)
        # ofac_clear stays True only if the LLM said so AND no hit.
        result["ofac_clear"] = bool(result.get("ofac_clear")) and not screen["has_sanctions_hit"]

    # ── High-risk geography — enhanced due diligence ─────────────────
    if screen["high_risk_country"]:
        result["requires_compliance_review"] = True
        try:
            result["risk_score"] = max(float(result.get("risk_score") or 0), 0.40)
        except (TypeError, ValueError):
            result["risk_score"] = 0.40
        if result.get("overall_status") not in ("rejected",):
            result["overall_status"] = "conditional"

    # ── Onboarding required-field + tax-form floor ───────────────────
    if onboarding:
        missing = list(result.get("missing_fields") or [])
        for field in _ONBOARDING_REQUIRED:
            val = result.get(field)
            if val in (None, "", False) and field not in missing:
                missing.append(field)
        # Country may be expressed as country_code OR jurisdiction.
        if not (result.get("country_code") or result.get("jurisdiction")) \
                and "country_code" not in missing:
            missing.append("country_code")
        if screen["missing_tax_form"] and "tax_form_kind" not in missing:
            missing.append("tax_form_kind")
        result["missing_fields"] = missing
        if missing:
            result["requires_human_review"] = True
    else:
        # kyc_only — leave onboarding fields at safe defaults so the
        # invoice-chain edge never fires for a non-vendor counterparty.
        result.setdefault("missing_fields", [])
        result["requires_human_review"] = bool(result.get("requires_human_review"))

    # ── Compliance-review escalation when not already forced ─────────
    if screen["forces_compliance_review"]:
        result["requires_compliance_review"] = True

    # ── onboarded_to_master is allowed True ONLY on a fully clean path ─
    clean = (
        result.get("overall_status") == "approved"
        and not result.get("missing_fields")
        and bool(result.get("ofac_clear"))
        and not screen["forces_compliance_review"]
    )
    result["onboarded_to_master"] = bool(result.get("onboarded_to_master")) and clean

    # requires_human_review must be set whenever the counterparty is not a
    # clean, fully-onboarded vendor in onboarding mode; a clean onboard
    # explicitly clears it.
    if onboarding:
        result["requires_human_review"] = not result["onboarded_to_master"]

    return result


@pre_compose(context_processor=load_vendor_context)
@chain(enforce_compliance_floor)
async def vendor_compliance_agent(
    llm_response: dict[str, Any] | str | None,
    input_data: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Surface the LLM's KYC / onboarding verdict; the chain enforces the
    sanctions / geography / tax-form floor."""
    input_data = input_data if isinstance(input_data, dict) else {}
    result = _parse(llm_response)
    if not isinstance(result, dict):
        result = {}
    result = dict(result)

    # Resolve the mode (explicit input wins, else the LLM's, else infer).
    mode = str(
        _up(input_data, "compliance_mode", "")
        or result.get("compliance_mode")
        or ""
    ).strip().lower()
    if mode not in ("onboarding", "kyc_only"):
        # A vendor profile (vendor_id / tax_form_kind present) ⇒ onboarding.
        has_vendor_profile = bool(result.get("vendor_id") or result.get("tax_form_kind"))
        mode = "onboarding" if has_vendor_profile else "kyc_only"

    # Resolve the country: prefer the LLM's jurisdiction, then the on-file
    # vendor record from @pre_compose (self-reliant on a direct call too).
    cctx = _ctx(context)
    country = str(result.get("jurisdiction") or "")
    if not country:
        country = str(cctx.get("on_file_country") or "")
        if not country:
            vid = str(_up(input_data, "counterparty_id", "") or _up(input_data, "vendor_id", "") or "")
            if vid:
                country = str(store.get_vendor(vid).get("country_code") or "")
        if country:
            result["jurisdiction"] = country

    # Private carry → consumed + stripped by enforce_compliance_floor.
    result["_cmp_mode"] = mode
    result["_cmp_legal_name"] = str(result.get("legal_name") or "")
    result["_cmp_country"] = country
    result["_cmp_tax_form"] = str(result.get("tax_form_kind") or "")
    return result
